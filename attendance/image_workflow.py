"""Image-based attendance workflow.

Flow:
  1. Load config and prepare image (preprocess.py)
  2. OCR with 2-pass logic and fallback (ocr.py)
  3. Delegate to row_processor.process_rows() for matching, gating, writing

Entry points:
  - run(): Process single image
  - run_multi(): Process multiple images as one upload
"""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import yaml

from attendance import health as health_mod
from attendance.normalizer import normalize_for_matching
from attendance.ocr import OcrRow, OcrUnavailableError, build_provider
from attendance.photos import merge_photo_rows
from attendance.preprocess import prepare
from attendance.row_processor import process_rows
from attendance.session_resolver import SheetSession, resolve
from attendance.utils.progress import ProgressFn, STAGE_READING, _emit
from attendance.utils.upload_utils import generate_upload_id
from core.logger import get_logger
from core.notifier import RunSummary

log = get_logger("image_workflow")


def load_config(path: str = "config/attendance.yaml") -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        raise RuntimeError(f"Configuration file not found: {path}")
    from attendance.config_model import ConfigError, validate_cfg
    try:
        validate_cfg(cfg)
    except ConfigError as exc:
        raise RuntimeError(str(exc)) from exc
    return cfg


def _second_pass_settings(ocr_cfg: dict) -> dict:
    """Effective ``attendance.ocr.second_pass`` settings. Unknown/missing -> today's behaviour (full)."""
    sp = (ocr_cfg or {}).get("second_pass") or {}
    if not isinstance(sp, dict):
        sp = {}
    mode = str(sp.get("mode", "full")).strip().lower()
    return {
        "mode": mode if mode in ("full", "low_confidence") else "full",
        "model": sp.get("model") or None,          # different Gemini model for the extra read
        "provider": sp.get("provider") or None,    # or a different provider (gemini | mock)
        "include_unmatched": bool(sp.get("include_unmatched", True)),
    }


def _needs_second_look(row, min_conf: float, roster_aware: bool, include_unmatched: bool) -> bool:
    """Row is worth a second read: confidence below min_confidence, or (roster-aware OCR) no roster match."""
    conf = getattr(row, "confidence", None)
    if isinstance(conf, (int, float)) and conf < min_conf:
        return True
    if roster_aware and include_unmatched and not getattr(row, "matched_roster_name", None):
        return True
    return False


def _avg_confidence(rows: list[OcrRow]) -> float:
    """Return the mean confidence over all rows (0.0 when empty)."""
    if not rows:
        return 0.0
    return sum(getattr(r, "confidence", 0.0) or 0.0 for r in rows) / len(rows)


def _read_sheet(*, dry_run: bool = False, **kwargs) -> list[OcrRow]:
    """OCR one sheet. Dry runs record no health events (see attendance/health.py).

    When ``ocr_cfg.fallback_to_gemini`` is ``true`` and the primary provider
    raises :exc:`~attendance.ocr.OcrUnavailableError`, the call is retried with
    :class:`~attendance.ocr.GeminiProvider`.
    """
    ocr_cfg: dict = kwargs.get("ocr_cfg", {})
    with health_mod.dry_run_scope(dry_run):
        try:
            return _read_sheet_inner(**kwargs)
        except OcrUnavailableError:
            if not ocr_cfg.get("fallback_to_gemini", False):
                raise
            log.warning(
                "ocr_primary_unavailable_falling_back_to_gemini",
                extra={"detail": "⚠️ Primary OCR unavailable — falling back to Gemini."},
            )
            from attendance.ocr import GeminiProvider
            gemini_provider = GeminiProvider(
                model=ocr_cfg.get("gemini_fallback_model", "gemini-3.1-flash-lite"),
                timeout_s=float(ocr_cfg.get("timeout_s", 60)),
                max_attempts=int(ocr_cfg.get("max_attempts", 3)),
            )
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["provider"] = gemini_provider
            return _read_sheet_inner(**fallback_kwargs)


def _read_sheet_inner(
    *,
    image_bytes: bytes,
    provider,
    roster_names: Optional[list[str]],
    ocr_cfg: dict,
    min_conf: float,
    upload_id: str,
    photo: Optional[int] = None,
    raw_bytes: Optional[bytes] = None,
    preprocess_cfg: Optional[dict] = None,
) -> list[OcrRow]:
    """OCR one sheet image (shared by run() and run_multi()).

    ``passes: 1``                      -> one read.
    ``passes: 2`` + ``mode: full``     -> every row read twice and merged (today's behaviour, the default).
    ``passes: 2`` + ``mode: low_confidence`` -> pass 1 on everything; a second read ONLY when some rows are
        low-confidence / unmatched, merged into just those rows (optionally with another model/provider).
    Pass-1 failure raises (caller reports it); a failed pass 2 is logged and pass 1 is used as-is.
    Retries and the 429 model fallback live inside ``provider.read`` (ocr.py / _net.py).

    ``retry_preprocessing: true`` (T8): when average confidence after all normal passes is still below
    ``min_confidence``, re-preprocess the *original* raw bytes with stronger settings
    (``contrast=clahe, adaptive_thresh=True``) and run the provider once more.  The result with the
    higher average confidence wins.  Disabled by default; set ``attendance.ocr.retry_preprocessing: true``.
    """
    extra = {"upload_id": upload_id} if photo is None else {"upload_id": upload_id, "photo": photo}
    log.info("ocr_pass_1", extra=extra)
    rows1 = provider.read(image_bytes, roster=roster_names)

    if int(ocr_cfg.get("passes", 2)) < 2:
        rows_final = rows1
    else:
        sp = _second_pass_settings(ocr_cfg)

        if sp["mode"] == "full":
            rows2: list[OcrRow] = []
            log.info("ocr_pass_2", extra=extra)
            try:
                rows2 = provider.read(image_bytes, roster=roster_names)
            except Exception as exc:
                log.warning("ocr_pass2_failed", extra={**extra, "error": str(exc)})
            rows_final = _merge_passes(rows1, rows2)

        else:
            # mode == "low_confidence"
            roster_aware = roster_names is not None
            flagged = [_needs_second_look(r, min_conf, roster_aware, sp["include_unmatched"]) for r in rows1]
            if not any(flagged):
                log.info("ocr_pass_2_skipped", extra={**extra, "rows": len(rows1)})
                rows_final = rows1
            else:
                log.info("ocr_pass_2_targeted", extra={**extra, "rows": len(rows1), "rechecked": sum(flagged)})
                try:
                    second = provider
                    if sp["model"] or sp["provider"]:
                        second_cfg = dict(ocr_cfg)
                        if sp["model"]:
                            second_cfg["model"] = sp["model"]
                        if sp["provider"]:
                            second_cfg["provider"] = sp["provider"]
                        second = build_provider(second_cfg)
                    rows2 = second.read(image_bytes, roster=roster_names)
                except Exception as exc:
                    log.warning("ocr_pass2_failed", extra={**extra, "error": str(exc)})
                    rows_final = rows1
                else:
                    rows_final = [_merge_passes([r], rows2)[0] if hit else r for r, hit in zip(rows1, flagged)]

    # ── T8: Retry with alternative preprocessing on persistently low confidence ──
    retry_cfg = ocr_cfg.get("retry_preprocessing", False)
    if retry_cfg and raw_bytes is not None:
        avg = _avg_confidence(rows_final)
        if avg < min_conf:
            log.info(
                "ocr_retry_preprocess",
                extra={**extra, "avg_conf": round(avg, 3), "min_conf": min_conf},
            )
            try:
                alt_preprocess_cfg = dict(preprocess_cfg or {})
                alt_preprocess_cfg.update({"contrast": "clahe", "adaptive_thresh": True})
                alt_bytes = prepare(
                    raw_bytes,
                    orient=alt_preprocess_cfg.get("orient", True),
                    deskew=alt_preprocess_cfg.get("deskew", True),
                    contrast="clahe",
                    adaptive_thresh=True,
                    max_side_px=int(alt_preprocess_cfg.get("max_side_px", 2000)),
                    strip_exif=alt_preprocess_cfg.get("strip_exif", True),
                )
                alt_rows = provider.read(alt_bytes, roster=roster_names)
                alt_avg = _avg_confidence(alt_rows)
                log.info(
                    "ocr_retry_preprocess_done",
                    extra={**extra, "orig_avg": round(avg, 3), "alt_avg": round(alt_avg, 3)},
                )
                if alt_avg > avg:
                    log.info("ocr_retry_preprocess_accepted", extra=extra)
                    rows_final = alt_rows
            except Exception as exc:
                log.warning("ocr_retry_preprocess_failed", extra={**extra, "error": str(exc)})

    return rows_final


def _merge_passes(pass1: list[OcrRow], pass2: list[OcrRow]) -> list[OcrRow]:
    """Merge two OCR passes.
    - If pass2 is empty, return pass1.
    - If names differ between passes for the same row index, downgrade confidence.
    - Otherwise average confidence.
    """
    if not pass2:
        return pass1

    p2_by_idx = {row.row_index: row for row in pass2}
    merged = []

    for row in pass1:
        p2 = p2_by_idx.get(row.row_index)
        if p2 is None:
            merged.append(row)
            continue

        # Compare normalised names
        n1 = normalize_for_matching(row.raw_name)
        n2 = normalize_for_matching(p2.raw_name)

        if n1 == n2:
            # Agreement: average confidence
            merged_conf = (row.confidence + p2.confidence) / 2
            # Roster match: keep it only if both passes agree (or only one offered
            # one); two different guesses on an agreed name is itself a signal of
            # ambiguity, so drop it and let the matcher's own gates handle it.
            if row.matched_roster_name and p2.matched_roster_name:
                n_m1 = normalize_for_matching(row.matched_roster_name)
                n_m2 = normalize_for_matching(p2.matched_roster_name)
                matched = row.matched_roster_name if n_m1 == n_m2 else None
            else:
                matched = row.matched_roster_name or p2.matched_roster_name
            merged.append(OcrRow(
                raw_name=row.raw_name,
                status_mark=row.status_mark,
                confidence=merged_conf,
                row_index=row.row_index,
                bounding_box=row.bounding_box or p2.bounding_box,
                matched_roster_name=matched,
            ))
        else:
            # Disagreement: downgrade confidence below min_confidence
            log.warning(
                "ocr_pass_disagree",
                extra={
                    "row": row.row_index,
                    "pass1": row.raw_name,
                    "pass2": p2.raw_name,
                },
            )
            merged.append(OcrRow(
                raw_name=row.raw_name,
                status_mark=row.status_mark,
                confidence=min(row.confidence, p2.confidence) * 0.5,  # force below threshold
                row_index=row.row_index,
                bounding_box=row.bounding_box or p2.bounding_box,
                matched_roster_name=None,
            ))

    return merged


def run(
    *,
    image_path: str,
    course: str,
    session_num: int,
    session_type: Optional[str] = None,
    dry_run: bool = True,
    force: bool = False,
    config_path: str = "config/attendance.yaml",
    review_store_dir: str = "review_queue",
    progress: Optional[ProgressFn] = None,
) -> RunSummary:
    """Execute the full attendance workflow for a single image."""
    cfg = load_config(config_path)
    health_mod.configure(cfg)
    att_cfg = cfg.get("attendance", {})
    ocr_cfg = att_cfg.get("ocr", {})
    preprocess_cfg = ocr_cfg.get("preprocess", {})

    try:
        raw_bytes = Path(image_path).read_bytes()
        upload_id = generate_upload_id(raw_bytes, course, session_num)
    except FileNotFoundError:
        upload_id = "error_missing_file"
        summary = RunSummary(upload_id=upload_id, course=course, session=str(session_num), dry_run=dry_run)
        summary.errors.append(f"Image file not found: {image_path}")
        return summary
    except Exception as e:
        upload_id = "error_reading_file"
        summary = RunSummary(upload_id=upload_id, course=course, session=str(session_num), dry_run=dry_run)
        summary.errors.append(f"Failed to read image file: {str(e)}")
        return summary

    log.info("workflow_start", extra={"upload_id": upload_id, "image": image_path, "dry_run": dry_run})
    _emit(progress, STAGE_READING)

    summary = RunSummary(
        upload_id=upload_id,
        course=course,
        session=str(session_num),
        dry_run=dry_run,
    )

    image_bytes = prepare(
        raw_bytes,
        orient=preprocess_cfg.get("orient", True),
        deskew=preprocess_cfg.get("deskew", True),
        contrast=preprocess_cfg.get("contrast", "mild"),
        max_side_px=int(preprocess_cfg.get("max_side_px", 2000)),
        strip_exif=preprocess_cfg.get("strip_exif", True),
    )

    provider = build_provider(ocr_cfg)
    min_conf = float(ocr_cfg.get("min_confidence", 0.75))

    # Resolve the session/roster up front (instead of after OCR) when the OCR
    # step is allowed to see the roster — it needs the real names *before* it
    # reads the photo to have any chance of telling similar students apart.
    session_info: Optional[SheetSession] = None
    roster_names: Optional[list[str]] = None
    if ocr_cfg.get("send_roster_to_provider", False):
        try:
            session_info = resolve(course_key=course, session_num=session_num, session_type=session_type, cfg=cfg)
            roster_names = [name for name, _ in session_info.roster]
        except Exception as exc:
            # Don't let an early resolve failure kill the run here — fall back to
            # resolving again (and surfacing the real error) inside process_rows.
            log.warning("early_session_resolve_failed", extra={"error": str(exc), "upload_id": upload_id})
            session_info = None
            roster_names = None

    try:
        ocr_rows = _read_sheet(image_bytes=image_bytes, provider=provider, roster_names=roster_names,
                               ocr_cfg=ocr_cfg, min_conf=min_conf, upload_id=upload_id, dry_run=dry_run,
                               raw_bytes=raw_bytes, preprocess_cfg=preprocess_cfg)
    except Exception as exc:
        log.error("ocr_api_error", extra={"error": str(exc), "upload_id": upload_id})
        summary.errors.append(f"API_ERROR: OCR pass 1 failed: {exc}")
        return summary

    return process_rows(
        ocr_rows=ocr_rows,
        course=course,
        session_num=session_num,
        session_type=session_type,
        dry_run=dry_run,
        force=force,
        cfg=cfg,
        min_conf=min_conf,
        upload_id=upload_id,
        image_path=image_path,
        review_store_dir=review_store_dir,
        summary=summary,
        session_info=session_info,
        progress=progress,
    )


def run_multi(
    *,
    image_paths: list[str],
    course: str,
    session_num: int,
    session_type: Optional[str] = None,
    dry_run: bool = True,
    force: bool = False,
    config_path: str = "config/attendance.yaml",
    review_store_dir: str = "review_queue",
    progress: Optional[ProgressFn] = None,
) -> RunSummary:
    """Run the workflow over SEVERAL photos of one attendance sheet as ONE upload.

    Each photo is OCR'd on its own, the rows are merged (duplicates across photos
    collapsed; conflicting marks sent to review), then everything is matched and
    written as a single session under one ``upload_id``.

    All-or-nothing on OCR: if any photo cannot be read, nothing is written. A missing
    photo would make its students look absent.
    """
    if not image_paths:
        raise ValueError("run_multi needs at least one image path")
    if len(image_paths) == 1:
        return run(
            image_path=image_paths[0], course=course, session_num=session_num,
            session_type=session_type, dry_run=dry_run, force=force,
            config_path=config_path, review_store_dir=review_store_dir,
        )

    cfg = load_config(config_path)
    health_mod.configure(cfg)
    att_cfg = cfg.get("attendance", {})
    ocr_cfg = att_cfg.get("ocr", {})
    preprocess_cfg = ocr_cfg.get("preprocess", {})

    raw_list: list[bytes] = []
    for path in image_paths:
        try:
            raw_list.append(Path(path).read_bytes())
        except FileNotFoundError:
            summary = RunSummary(upload_id="error_missing_file", course=course, session=str(session_num), dry_run=dry_run)
            summary.errors.append(f"Image file not found: {path}")
            return summary
        except Exception as e:
            summary = RunSummary(upload_id="error_reading_file", course=course, session=str(session_num), dry_run=dry_run)
            summary.errors.append(f"Failed to read image file: {str(e)}")
            return summary

    upload_id = hashlib.sha256(b"".join(raw_list) + course.encode() + str(session_num).encode()).hexdigest()[:16]
    log.info("workflow_multi_start", extra={"upload_id": upload_id, "photos": len(image_paths), "dry_run": dry_run})
    _emit(progress, STAGE_READING)

    summary = RunSummary(
        upload_id=upload_id, course=course, session=str(session_num),
        dry_run=dry_run, photo_count=len(image_paths),
    )

    provider = build_provider(ocr_cfg)
    min_conf = float(ocr_cfg.get("min_confidence", 0.75))

    session_info: Optional[SheetSession] = None
    roster_names: Optional[list[str]] = None
    if ocr_cfg.get("send_roster_to_provider", False):
        try:
            session_info = resolve(course_key=course, session_num=session_num, session_type=session_type, cfg=cfg)
            roster_names = [name for name, _ in session_info.roster]
        except Exception as exc:
            log.warning("early_session_resolve_failed", extra={"error": str(exc), "upload_id": upload_id})
            session_info = None
            roster_names = None

    per_photo: list[list[OcrRow]] = []

    def _process_one_photo(n: int, raw_bytes: bytes) -> Optional[list[OcrRow]]:
        """Process a single photo (preprocess + OCR). Called in parallel."""
        image_bytes = prepare(
            raw_bytes,
            orient=preprocess_cfg.get("orient", True),
            deskew=preprocess_cfg.get("deskew", True),
            contrast=preprocess_cfg.get("contrast", "mild"),
            max_side_px=int(preprocess_cfg.get("max_side_px", 2000)),
            strip_exif=preprocess_cfg.get("strip_exif", True),
        )
        log.info("ocr_photo", extra={"upload_id": upload_id, "photo": n, "of": len(raw_list)})
        try:
            return _read_sheet(image_bytes=image_bytes, provider=provider, roster_names=roster_names,
                               ocr_cfg=ocr_cfg, min_conf=min_conf, upload_id=upload_id, photo=n,
                               dry_run=dry_run, raw_bytes=raw_bytes, preprocess_cfg=preprocess_cfg)
        except Exception as exc:
            log.error("ocr_api_error", extra={"error": str(exc), "upload_id": upload_id, "photo": n})
            raise exc

    # Process photos concurrently (up to 4 at a time to avoid overwhelming the OCR API)
    max_workers = min(4, len(raw_list))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(_process_one_photo, n, raw): n
            for n, raw in enumerate(raw_list, start=1)
        }
        for future in as_completed(future_to_idx):
            n = future_to_idx[future]
            try:
                result = future.result()
                per_photo.append((n, result))
            except Exception as exc:
                summary.errors.append(f"API_ERROR: OCR failed on photo {n} of {len(raw_list)}: {exc}")
                return summary

    # Sort by original photo order
    per_photo.sort(key=lambda x: x[0])
    per_photo = [rows for _, rows in per_photo]

    ocr_rows = merge_photo_rows(per_photo, list(image_paths))

    return process_rows(
        ocr_rows=ocr_rows,
        course=course,
        session_num=session_num,
        session_type=session_type,
        dry_run=dry_run,
        force=force,
        cfg=cfg,
        min_conf=min_conf,
        upload_id=upload_id,
        image_path=image_paths[0],
        review_store_dir=review_store_dir,
        summary=summary,
        session_info=session_info,
        progress=progress,
    )
