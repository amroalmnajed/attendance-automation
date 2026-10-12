"""attendance/cli.py — Command-line interface for Sub-System 1.

Usage:
  # ── Recommended daily use: interactive wizard (no flags needed) ──
  python -m attendance.cli attend
  # Asks: image or paste names? → course → session → type → dry-run → go

  # Process a single photo in dry-run mode (safe, no writes):
  python -m attendance.cli process --image path/to/photo.jpg --course g10 --session 5 --type "Saturday - Center"

  # Process with actual write:
  python -m attendance.cli process --image photo.jpg --course crash --session 3 --no-dry-run

  # Force overwrite existing cells:
  python -m attendance.cli process --image photo.jpg --course main --session 12 --no-dry-run --force

  # Process a text file of already-extracted names (fully interactive — will prompt for missing args):
  python -m attendance.cli process-text

  # Process text file with all args provided (no prompts):
  python -m attendance.cli process-text --text names.txt --course g10 --session 5 --type "Saturday - Center" --no-dry-run

  # Each line may carry a mark: "Ahmed Ali | L", "Sara - absent", "Omar, Late"
  # (P/L/A/NC or Present/Late/Absent/Not complete; English or Arabic; no mark = Present)

  # Dry-run text import (safe, no writes):
  python -m attendance.cli process-text --text names.txt --course crash --session 3

  # List all pending review items:
  python -m attendance.cli review list

  # Resolve a flagged item:
  python -m attendance.cli review resolve --upload-id abc123 --item-id def456 --roster-row 7 --value Present

  # Test OCR on a folder of images (no Sheets connection needed):
  python -m attendance.cli ocr-test --folder C:\\Users\\lenovo\\Desktop\\agy-project-help

  # Run full dry-run on the sample photos from the handoff:
  python -m attendance.cli process --image "C:\\Users\\lenovo\\Desktop\\agy-project-help\\actualattendancephoto1.jpeg" --course g10 --session 1 --type "Saturday - Center" --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(override=True)

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import yaml

from core.logger import get_logger
from core.notifier import send

log = get_logger("cli")


def _load_cfg(path: str = "config/attendance.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── Subcommand: process ──────────────────────────────────────────────────────

def cmd_process(args: argparse.Namespace) -> None:
    from attendance.pipeline import run

    cfg = _load_cfg(args.config)
    dry_run = args.dry_run

    if dry_run:
        print("\n[DRY-RUN MODE] No data will be written to Google Sheets.\n")

    try:
        summary = run(
            image_path=args.image,
            course=args.course,
            session_num=args.session,
            session_type=args.type,
            dry_run=dry_run,
            force=args.force,
            config_path=args.config,
            review_store_dir=cfg["attendance"]["review"].get("local_dir", "review_queue"),
        )
    except Exception as exc:
        log.error("pipeline_error", extra={"error": str(exc)})
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    notify_cfg = cfg.get("core", {}).get("notify", {})
    send(summary, channel=notify_cfg.get("channel", "log"))

    print(summary.text())

    if summary.flagged > 0:
        print(f"\n{summary.flagged} item(s) need review. Run:")
        print(f"  python -m attendance.cli review list\n")


# ─── Subcommand: attend (interactive wizard) ──────────────────────────────────

def _ask(prompt: str, choices: list[str] | None = None) -> str:
    """Prompt the user and validate against choices if provided."""
    while True:
        answer = input(prompt).strip()
        if not answer:
            print("  [!] Answer cannot be empty, try again.")
            continue
        if choices and answer.lower() not in [c.lower() for c in choices]:
            print(f"  [!] Please enter one of: {', '.join(choices)}")
            continue
        return answer


def cmd_attend(args: argparse.Namespace) -> None:
    """Unified interactive wizard: image OCR  OR  paste names directly."""
    from attendance.pipeline import run, run_text

    cfg = _load_cfg(args.config)
    review_dir = cfg["attendance"]["review"].get("local_dir", "review_queue")
    notify_cfg = cfg.get("core", {}).get("notify", {})

    print("\n" + "─" * 60)
    print("  Attendance Wizard")
    print("─" * 60)

    # ── Step 1: Input mode ────────────────────────────────────────
    mode = _ask(
        "\nHow do you want to provide attendance data?\n"
        "  [i] I have an image / photo of the sheet\n"
        "  [n] I'll paste the names directly\n"
        "Choice (i/n): ",
        choices=["i", "n"],
    )

    # ── Step 2: Course ────────────────────────────────────────────
    valid_courses = ["crash", "main", "g10", "g9"]
    course = _ask(
        f"\nCourse ({'/'.join(valid_courses)}): ",
        choices=valid_courses,
    )

    # ── Step 3: Session number ────────────────────────────────────
    while True:
        try:
            session = int(_ask("\nSession number (e.g. 5): "))
            break
        except ValueError:
            print("  [!] Please enter a valid integer.")

    # ── Step 4: Session type (typed_pair courses only) ────────────
    session_type: str | None = None
    if course in ("g10", "g9"):
        course_types_cfg = cfg["attendance"]["courses"][course].get("types", {})
        # Flatten all allowed type strings for this course
        all_types: list[str] = []
        for bucket in course_types_cfg.values():
            all_types.extend(bucket)
        print(f"\nAvailable session types for {course}:")
        for i, t in enumerate(all_types, 1):
            print(f"  {i}. {t}")
        session_type = _ask("\nPaste or type the session type exactly as shown above: ")

    # ── Step 5: Dry-run? ──────────────────────────────────────────
    dr_answer = _ask(
        "\nDry-run mode? (no data will be written)\n"
        "  [y] Yes, just show me what would happen\n"
        "  [n] No, write to Google Sheets for real\n"
        "Choice (y/n): ",
        choices=["y", "n"],
    )
    dry_run = dr_answer.lower() == "y"

    # ── Step 6: Force overwrite? (only ask when writing for real) ─
    force = False
    if not dry_run:
        force_answer = _ask(
            "\nOverwrite cells that already have a value?\n"
            "  [y] Yes — overwrite existing entries\n"
            "  [n] No  — skip already-filled cells (safe default)\n"
            "Choice (y/n): ",
            choices=["y", "n"],
        )
        force = force_answer.lower() == "y"

    print()
    if dry_run:
        print("[DRY-RUN MODE] No data will be written to Google Sheets.\n")

    # ── Step 7a: Image path ───────────────────────────────────────
    if mode == "i":
        image_path = _ask("Image/photo path: ")
        if not os.path.exists(image_path):
            print(f"\n[ERROR] File not found: {image_path}", file=sys.stderr)
            sys.exit(1)

        try:
            summary = run(
                image_path=image_path,
                course=course,
                session_num=session,
                session_type=session_type,
                dry_run=dry_run,
                force=force,
                config_path=args.config,
                review_store_dir=review_dir,
            )
        except Exception as exc:
            log.error("pipeline_error", extra={"error": str(exc)})
            print(f"\n[ERROR] {exc}", file=sys.stderr)
            sys.exit(1)

    # ── Step 7b: Paste names inline ───────────────────────────────
    else:
        print("Paste names below — one name per line.")
        print("Optional mark per line, e.g.  Ahmed Ali | L   ·   Sara - absent   ·   Omar, Late")
        print("(P/Present, L/Late, A/Absent, NC/Not complete — no mark means Present.)")
        print("When you're done, press Enter on an empty line.\n")
        names: list[str] = []
        while True:
            try:
                line = input("  > ").strip()
            except EOFError:
                break
            if not line:
                break
            names.append(line)

        if not names:
            print("\n[ERROR] No names entered. Aborting.", file=sys.stderr)
            sys.exit(1)

        print(f"\n  {len(names)} line(s) collected.")
        names, marks = _parse_marks_with_preview(names, always_confirm=True, assume_yes=False)
        print("\n  Processing…\n")

        try:
            summary = run_text(
                names=names,
                marks=marks,
                course=course,
                session_num=session,
                session_type=session_type,
                dry_run=dry_run,
                force=force,
                config_path=args.config,
                review_store_dir=review_dir,
            )
        except Exception as exc:
            log.error("pipeline_error", extra={"error": str(exc)})
            print(f"\n[ERROR] {exc}", file=sys.stderr)
            sys.exit(1)

    send(summary, channel=notify_cfg.get("channel", "log"))
    print(summary.text())

    if summary.flagged > 0:
        print(f"\n{summary.flagged} item(s) need review. Run:")
        print(f"  python -m attendance.cli review list\n")



def _parse_marks_with_preview(lines: list[str], *, always_confirm: bool, assume_yes: bool) -> tuple[list[str], list[str]]:
    """Parse "name | mark" lines, print how each was read, and (when needed) confirm.

    Returns (names, marks). Exits cleanly if the user declines. Unrecognised marks are
    never guessed: they are passed through so the pipeline sends them to review.
    """
    from attendance.marks import format_preview, has_issues, parse_lines, split_names_and_marks

    parsed = parse_lines(lines)
    if not parsed:
        print("\n[ERROR] No names found. Aborting.", file=sys.stderr)
        sys.exit(1)

    print("\n" + format_preview(parsed) + "\n")

    needs_confirm = always_confirm or has_issues(parsed)
    if needs_confirm and not assume_yes and sys.stdin.isatty():
        if has_issues(parsed):
            print("Lines marked ⚠️ will go to the review queue instead of being written.")
        answer = _ask("Does this look right? Process now (y/n): ", choices=["y", "n"])
        if answer.lower() != "y":
            print("\nCancelled. Nothing was processed.")
            sys.exit(0)
    return split_names_and_marks(parsed)


def cmd_process_text(args: argparse.Namespace) -> None:
    from attendance.pipeline import run_text
    
    cfg = _load_cfg(args.config)
    
    # Interactive Prompts for Missing Args
    course = args.course
    if not course:
        course = input("Enter course (crash, main, g10, g9): ").strip()
    
    session = args.session
    if session is None:
        while True:
            try:
                session = int(input("Enter session number (e.g. 1): ").strip())
                break
            except ValueError:
                print("Invalid number. Please try again.")
    
    session_type = args.type
    if course in ("g10", "g9") and not session_type:
        session_type = input("Enter session type (e.g. 'Saturday - Center'): ").strip()
        
    dry_run = args.dry_run
    
    if not args.text:
        text_file = input("Enter path to text file containing names: ").strip()
    else:
        text_file = args.text
        
    if not os.path.exists(text_file):
        print(f"\n[ERROR] File not found: {text_file}", file=sys.stderr)
        sys.exit(1)
        
    with open(text_file, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    # Each line may carry a mark: "Ahmed Ali | L", "Sara - absent", "Omar, Late".
    names, marks = _parse_marks_with_preview(lines, always_confirm=False, assume_yes=getattr(args, "yes", False))

    if dry_run:
        print("\n[DRY-RUN MODE] No data will be written to Google Sheets.\n")

    try:
        summary = run_text(
            names=names,
            marks=marks,
            course=course,
            session_num=session,
            session_type=session_type,
            dry_run=dry_run,
            force=args.force,
            config_path=args.config,
            review_store_dir=cfg["attendance"]["review"].get("local_dir", "review_queue"),
        )
    except Exception as exc:
        log.error("pipeline_error", extra={"error": str(exc)})
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    notify_cfg = cfg.get("core", {}).get("notify", {})
    send(summary, channel=notify_cfg.get("channel", "log"))

    print(summary.text())

    if summary.flagged > 0:
        print(f"\n{summary.flagged} item(s) need review. Run:")
        print(f"  python -m attendance.cli review list\n")

# ─── Subcommand: review ───────────────────────────────────────────────────────

def cmd_review_list(args: argparse.Namespace) -> None:
    from attendance.review_queue import ReviewQueue
    rq = ReviewQueue(store_dir=args.store_dir)
    rq.print_pending(upload_id=getattr(args, "upload_id", None))


def cmd_review_resolve(args: argparse.Namespace) -> None:
    from attendance.aliases import AliasStore
    from attendance.review_queue import ReviewQueue
    from attendance.session_resolver import resolve
    from attendance.writer import WriteCell, batch_write

    cfg = _load_cfg(args.config)
    rq = ReviewQueue(store_dir=cfg["attendance"]["review"].get("local_dir", "review_queue"))

    item = rq.get(args.upload_id, args.item_id)
    if item is None:
        sys.exit(f"Item {args.item_id} not found in upload {args.upload_id}")
    if item.get("status") == "resolved":
        sys.exit(f"Item {args.item_id} already resolved")

    course = item["course"]
    session_info = resolve(
        course_key=course,
        session_num=item["session"],
        session_type=None,
        cfg=cfg,
    )
    roster_name = args.roster_name or f"row {args.roster_row}"

    # Write the sheet FIRST; only mark resolved once the write succeeded (raises on failure),
    # so a failed write leaves the item pending instead of silently lost.
    write_result = batch_write(
        session_info,
        [WriteCell(
            roster_name=roster_name,
            sheet_row=args.roster_row,
            col=session_info.session_col,
            old_value=None,
            new_value=args.value,
        )],
        upload_id=args.upload_id,
        dry_run=args.dry_run,
        retry_cfg=cfg["attendance"]["write"].get("retry", {}),
    )

    if args.dry_run:
        print("No cells written (dry-run).")
        return

    rq.resolve(
        upload_id=args.upload_id,
        item_id=args.item_id,
        chosen_roster_row=args.roster_row,
        chosen_roster_name=roster_name,
        attendance_value=args.value,
        resolved_by="cli",
    )

    # Learn: this paper spelling → this student (skipped for "Ignore", row 0).
    match_cfg = cfg["attendance"].get("match", {})
    if args.roster_row > 0 and match_cfg.get("learn_aliases", True):
        AliasStore(
            path=match_cfg.get("aliases_file", "config/learned_aliases.json"),
            min_confirmations=int(match_cfg.get("alias_min_confirmations", 1)),
        ).learn(course, item["raw_name"], roster_name)
    # Feature 15: spelling-variant SUGGESTION (separate file; only a human-approved one ever changes matching).
    if args.roster_row > 0 and not args.dry_run:
        from attendance import variants as variants_mod
        variants_mod.record_resolution(cfg, course, item["raw_name"], roster_name)

    # Write type cell for typed_pair courses (g9/g10)
    session_type = getattr(args, "session_type", None) or item.get("session_type")
    if not args.dry_run and session_type and session_info.type_col:
        from attendance.writer import WriteCell, batch_write
        batch_write(
            session_info,
            [WriteCell(
                roster_name=roster_name,
                sheet_row=args.roster_row,
                col=session_info.type_col,
                old_value=None,
                new_value=session_type,
            )],
            upload_id=args.upload_id,
            dry_run=False,
            retry_cfg=cfg["attendance"]["write"].get("retry", {}),
        )

    if write_result.written or write_result.skipped:
        print(f"✓ Resolved: row {args.roster_row} → '{args.value}'" + (f" (type: {session_type})" if session_type else ""))
    else:
        print("Resolved; no cell change needed.")


def cmd_review_cleanup(args: argparse.Namespace) -> None:
    """Delete resolved review images older than retention period."""
    from attendance.review_queue import ReviewQueue

    cfg = _load_cfg(args.config)
    rq = ReviewQueue(store_dir=cfg["attendance"]["review"].get("local_dir", "review_queue"))
    deleted = rq.cleanup_resolved_images(review_images_dir=args.images_dir)
    print(f"Cleaned up {deleted} image(s) from {args.images_dir}")


# ─── Subcommand: ocr-test ─────────────────────────────────────────────────────

def cmd_ocr_test(args: argparse.Namespace) -> None:
    """Run OCR on a folder of images and print results. No Sheets connection needed."""
    from attendance.ocr import build_provider
    from attendance.normalizer import normalize_for_matching
    from attendance.preprocess import prepare

    cfg = _load_cfg(args.config)
    ocr_cfg = cfg["attendance"]["ocr"]
    prep_cfg = ocr_cfg.get("preprocess", {})

    folder = Path(args.folder)
    images = list(folder.glob("*.jpg")) + list(folder.glob("*.jpeg")) + list(folder.glob("*.png"))

    if not images:
        print(f"No images found in {folder}")
        return

    provider = build_provider(ocr_cfg)

    for img_path in sorted(images):
        print(f"\n{'='*60}")
        print(f"Image: {img_path.name}")
        print(f"{'='*60}")

        try:
            raw_bytes = img_path.read_bytes()
            img_bytes = prepare(
                raw_bytes,
                orient=prep_cfg.get("orient", True),
                deskew=prep_cfg.get("deskew", True),
                contrast=prep_cfg.get("contrast", "mild"),
                max_side_px=int(prep_cfg.get("max_side_px", 2000)),
                strip_exif=prep_cfg.get("strip_exif", True),
            )
            rows = provider.read(img_bytes)
            print(f"  Rows extracted: {len(rows)}")
            print(f"  {'#':>3}  {'Conf':>5}  {'Raw name':<35}  {'Mark'}")
            print(f"  {'-'*3}  {'-'*5}  {'-'*35}  {'-'*15}")
            for row in rows:
                latin = normalize_for_matching(row.raw_name)
                print(
                    f"  {row.row_index:>3}  {row.confidence:.2f}  "
                    f"{row.raw_name:<35}  {row.status_mark or '—'}"
                )
                print(f"         → normalized: {latin}")
        except Exception as exc:
            print(f"  ERROR: {exc}")


def _attendance_value(raw: str) -> str:
    """argparse type: canonicalise raw marks (P, ✓, absent...) or reject."""
    from attendance.normalizer import normalize_attendance_mark
    val = normalize_attendance_mark(raw)
    if val is None:
        raise argparse.ArgumentTypeError(f"unrecognised attendance value: {raw!r}")
    return val


# ─── Subcommand: aliases list / remove / export ───────────────────────────────

def _alias_store(args: argparse.Namespace):
    from attendance.aliases import AliasStore
    return AliasStore(path=args.alias_path)


def cmd_aliases_list(args: argparse.Namespace) -> None:
    rows = _alias_store(args).entries(stale_days=args.stale_days)
    if args.course:
        rows = [r for r in rows if r["course"].lower() == args.course.lower()]
    if args.flagged_only:
        rows = [r for r in rows if set(r["flags"]) - {"undated"}]
    if not rows:
        print("No aliases found." if not args.flagged_only else "No flagged aliases.")
        return
    print(f"\n  {'Course':<7} {'Paper name':<28} {'Roster name':<28} {'Seen':<5} {'Last used/confirmed':<20} Flags")
    for r in sorted(rows, key=lambda r: (r["course"], r["key"])):
        last = r["last_used"] or r["last_confirmed"] or "-"
        print(f"  {r['course']:<7} {r['key'][:27]:<28} {str(r['roster_name'])[:27]:<28} "
              f"{r['confirmations']:<5} {last:<20} {', '.join(r['flags'])}")
    print(f"\n  {len(rows)} alias(es). Flags: old/unused = not applied for {args.stale_days}+ days; "
          "undated = learned before dates were tracked; conflict/low = not currently applied; "
          "unknown-name = points at a placeholder.")


def cmd_aliases_remove(args: argparse.Namespace) -> None:
    store = _alias_store(args)
    key_rows = [r for r in store.entries() if r["course"].lower() == args.course.lower()]
    from attendance.normalizer import normalize_for_matching
    key = normalize_for_matching(args.name)
    match = [r for r in key_rows if r["key"] == key]
    if not match:
        print(f"No alias for “{args.name}” in course {args.course}. (Use `aliases list` to see the exact names.)")
        return
    r = match[0]
    targets = {r["roster_name"], *r["other_names"]} if not args.roster_name else {args.roster_name}
    print(f"Will remove: [{r['course']}] {r['key']} → {', '.join(sorted(map(str, targets)))}")
    if not args.yes and input("Remove it? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled; nothing changed.")
        return
    n = store.remove(r["course"], args.name, roster_name=args.roster_name)
    print(f"Removed {n} entr{'y' if n == 1 else 'ies'}. A backup copy was saved next to the alias file (.bak).")


def cmd_aliases_export(args: argparse.Namespace) -> None:
    import csv
    import json as _json
    rows = _alias_store(args).entries(stale_days=args.stale_days)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".json":
        out.write_text(_json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        with out.open("w", newline="", encoding="utf-8-sig") as fh:     # utf-8-sig: Excel shows Arabic correctly
            w = csv.writer(fh)
            w.writerow(["course", "paper_name", "roster_name", "confirmations", "other_names",
                        "first_seen", "last_confirmed", "last_used", "flags"])
            for r in rows:
                w.writerow([r["course"], r["key"], r["roster_name"], r["confirmations"],
                            "; ".join(f"{n} x{v}" for n, v in r["other_names"].items()),
                            r["first_seen"], r["last_confirmed"], r["last_used"], " ".join(r["flags"])])
    print(f"Exported {len(rows)} alias(es) to {out}")


# ─── Subcommand: aliases variants (Feature 15) ────────────────────────────────

def _variants_ctx(args: argparse.Namespace):
    from attendance import variants as v
    try:
        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    s = v.settings(cfg)
    return v, v.VariantSuggestions(args.suggestions_file or s["file"]), args.variants_file or s["variants_file"], s


def _show_variant(i: int, n: int, it: dict) -> None:
    print(f"\n[{i}/{n}]  “{it['paper_token']}”  →  “{it['roster_token']}”   (seen {it['count']}x, "
          f"courses: {', '.join(it.get('courses') or ['?'])})")
    print(f"        example: {it.get('example', '')}")


def cmd_variants_list(args: argparse.Namespace) -> None:
    v, sug, _vf, s = _variants_ctx(args)
    if args.from_aliases:
        from attendance.normalizer import suggest_variants_from_aliases
        n = sug.import_pairs(suggest_variants_from_aliases(alias_path=args.alias_path, min_confirmations=1), s["min_similarity"])
        print(f"Imported {n} suggestion(s) from the alias store.")
    items = sug.pending(args.min_count)
    if not items:
        print("No pending variant suggestions." + ("" if args.from_aliases else " (Use --from-aliases to also scan the alias store.)"))
        return
    for i, it in enumerate(items, 1):
        _show_variant(i, len(items), it)
    print("\nNothing has been applied. Run `aliases variants review` to approve or reject these.")


def cmd_variants_review(args: argparse.Namespace) -> None:
    v, sug, vfile, s = _variants_ctx(args)
    if args.from_aliases:
        from attendance.normalizer import suggest_variants_from_aliases
        sug.import_pairs(suggest_variants_from_aliases(alias_path=args.alias_path, min_confirmations=1), s["min_similarity"])
    items = sug.pending(args.min_count)
    if not items:
        print("No pending variant suggestions.")
        return
    added = 0
    for i, it in enumerate(items, 1):
        _show_variant(i, len(items), it)
        print("   Only approve if these are the SAME name spelled two ways (e.g. Gidaa/Gaidaa), not two different names.")
        ans = input("   [a]pprove / [r]eject / [s]kip / [q]uit ? ").strip().lower()
        if ans in ("q", "quit"):
            break
        if ans in ("a", "approve"):
            res = v.approve(it["paper_token"], it["roster_token"], vfile)
            if res.startswith("conflict"):
                print(f"   Not added: “{it['paper_token']}” is already used as a spelling of “{res.split(':', 1)[1]}”.")
                continue
            sug.set_status(it["paper_token"], it["roster_token"], "approved")
            added += res == "added"
            print("   Approved." if res == "added" else "   Already known; marked approved.")
        elif ans in ("r", "reject"):
            sug.set_status(it["paper_token"], it["roster_token"], "rejected")
            print("   Rejected (will not be suggested again).")
    if added:
        print(f"\n{added} variant(s) added to {vfile} (backup: .bak). RESTART the bot for them to take effect.")


# ─── Subcommand: aliases ──────────────────────────────────────────────────────

def cmd_aliases_suggest(args: argparse.Namespace) -> None:
    """Print name_variants.yaml candidates derived from accumulated human-review decisions.

    Each resolved review item teaches the alias store one paper-spelling → roster-name mapping.
    After a few real sessions this command surfaces token pairs that diverge consistently, i.e.
    good candidates to fold into config/name_variants.yaml so they auto-accept next time.
    """
    from attendance.normalizer import suggest_variants_from_aliases

    suggestions = suggest_variants_from_aliases(
        alias_path=args.alias_path,
        min_confirmations=args.min_confirmations,
    )

    if not suggestions:
        print("No variant suggestions found.")
        print(
            f"  (Need at least --min-confirmations={args.min_confirmations} confirmations per alias "
            "— keep using the bot and run this again after a few sessions.)"
        )
        return

    print(f"\n{'─'*72}")
    print(f"  Name Variant Suggestions  (min_confirmations={args.min_confirmations})")
    print(f"{'─'*72}")
    print(f"  {'Paper token':<20}  {'Roster token':<20}  {'Seen':<6}  Course / Example")
    print(f"  {'-'*20}  {'-'*20}  {'-'*6}  {'-'*30}")

    for s in suggestions:
        example = f"{s['raw_paper']} → {s['roster_name']}"
        print(
            f"  {s['paper_token']:<20}  {s['roster_token']:<20}  "
            f"{s['confirmations']:<6}  [{s['course']}] {example}"
        )

    print(f"{'─'*72}")
    print(
        "\nTo apply: add entries under the relevant section in config/name_variants.yaml, e.g.:\n"
        "\n  names:\n"
        "    # canonical: [variant, variant, ...]\n"
    )
    for s in suggestions:
        print(f"    {s['roster_token']}: [{s['paper_token']}]")
    print(
        "\nThen run: python tests/eval_matcher.py   # verify FAR stays 0%\n"
    )


# ─── Subcommand: report & revert ──────────────────────────────────────────────

def cmd_report_session(args: argparse.Namespace) -> None:
    from attendance.reporter import generate_session_report
    cfg = _load_cfg(args.config)
    report = generate_session_report(args.course, args.session, cfg, Path(args.store_dir))
    
    print(f"\n--- Session Report: {args.course.upper()} S{args.session} ---")
    print(f"Total Students: {report['total_students']}")
    print(f"Present:        {report['present']} ({report['attendance_rate']:.1f}%)")
    print(f"Review Backlog: {report['review_backlog']} items")
    print("\nAbsentees:")
    if report['absentees']:
        for name in report['absentees']:
            print(f"  - {name}")
    else:
        print("  (None)")
    print()


def cmd_report_streaks(args: argparse.Namespace) -> None:
    from attendance.reporter import generate_streaks_report
    cfg = _load_cfg(args.config)
    streaks = generate_streaks_report(args.course, args.consecutive, cfg)
    
    print(f"\n--- Chronic Absentees ({args.course.upper()} - past {args.consecutive} sessions) ---")
    if streaks:
        for s in streaks:
            print(f"  Row {s['row']:<3} | {s['name']}")
    else:
        print("  (None)")
    print()


def cmd_revert(args: argparse.Namespace) -> None:
    from attendance.reporter import read_audit_log, plan_revert
    from attendance.writer import WriteCell, batch_write
    
    audit_records = read_audit_log()
    plan = plan_revert(args.upload_id, audit_records)
    
    if not plan:
        print(f"No non-dry-run audit records found for upload_id {args.upload_id}")
        return
        
    # We must group the writes by course/session/tab to do batch_write
    # For simplicity, we assume an upload_id corresponds to a single course/session.
    # The audit record has course and cell. We can just use the first record's course.
    writes_by_target = [r for r in audit_records if r.upload_id == args.upload_id]
    if not writes_by_target:
        return
        
    course = writes_by_target[0].course
    # We can't easily get session_num from audit, but we can reconstruct it from the column?
    # Or just resolve the sheet and write directly using gspread?
    # Actually, batch_write requires a SheetSession, which needs course and session_num.
    # Since we know the sheet_id and tab from AuditRecord, we could construct a dummy SheetSession.
    cfg = _load_cfg(args.config)
    
    cells_to_write = []
    has_conflicts = False
    
    print(f"\nRevert Plan for upload_id: {args.upload_id}")
    print(f"  {'Cell':<5}  {'Current':<15}  {'Revert To':<15}  {'Status'}")
    print(f"  {'-'*5}  {'-'*15}  {'-'*15}  {'-'*15}")
    for p in plan:
        status = "OK"
        if p.overwritten_since:
            status = "OVERWRITTEN"
            has_conflicts = True
        
        print(f"  {p.cell:<5}  {p.current_in_audit or '(empty)':<15}  {p.revert_to_value or '(empty)':<15}  {status}")
        
        if p.overwritten_since and not args.force:
            continue
            
        cells_to_write.append(WriteCell(
            roster_name=f"Cell {p.cell}",
            sheet_row=p.sheet_row,
            col=p.col,
            old_value=p.current_in_audit,
            new_value=p.revert_to_value or "",
        ))
        
    if has_conflicts and not args.force:
        print("\nWARNING: Some cells were modified since this upload. Use --force to overwrite them anyway.")
        if not cells_to_write:
            return
            
    if args.dry_run:
        print("\n[DRY-RUN MODE] Run with --no-dry-run to apply revert.")
        return
        
    if not cells_to_write:
        print("Nothing to revert.")
        return
        
    # To use batch_write, we need a SheetSession.
    from attendance.session_resolver import SheetSession
    from core.auth import get_credentials
    import gspread
    
    gc = gspread.authorize(get_credentials())
    session_info = SheetSession(
        course=course,
        session_num=0, # dummy
        session_col="",
        type_col=None,
        roster=[],
        allowed_attendance=[],
        allowed_types=[],
        sheet_id=writes_by_target[0].sheet_id,
        tab=writes_by_target[0].tab,
        gc=gc
    )
    
    write_result = batch_write(
        session_info,
        cells_to_write,
        upload_id=f"rev-{args.upload_id}"[:16],
        dry_run=args.dry_run,
        retry_cfg=cfg["attendance"]["write"].get("retry", {}),
    )
    
    print(f"Reverted {len(write_result.written)} cells.")


# ─── Main entry ───────────────────────────────────────────────────────────────

def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    # Ensure stdout/stderr handle Unicode/Arabic characters on Windows
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf_8'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Attendance Automation — Sub-System 1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config/attendance.yaml", help="Config file path")

    sub = parser.add_subparsers(dest="command", required=True)

    # report
    p_rep = sub.add_parser("report", help="Generate reports")
    rep_sub = p_rep.add_subparsers(dest="report_cmd", required=True)
    
    p_rep_sess = rep_sub.add_parser("session", help="Session summary")
    p_rep_sess.add_argument("--course", required=True)
    p_rep_sess.add_argument("--session", required=True, type=int)
    p_rep_sess.add_argument("--store-dir", default="review_queue")
    p_rep_sess.set_defaults(func=cmd_report_session)
    
    p_rep_streaks = rep_sub.add_parser("streaks", help="Chronic absentee streaks")
    p_rep_streaks.add_argument("--course", required=True)
    p_rep_streaks.add_argument("--consecutive", type=int, default=3)
    p_rep_streaks.set_defaults(func=cmd_report_streaks)
    
    # revert
    p_revt = sub.add_parser("revert", help="Revert a previous upload")
    p_revt.add_argument("upload_id", help="Upload ID to revert")
    p_revt.add_argument("--dry-run", action="store_true", default=True, help="Dry-run (no writes)")
    p_revt.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    p_revt.add_argument("--force", action="store_true", default=False, help="Overwrite even if modified since")
    p_revt.set_defaults(func=cmd_revert)

    # attend (interactive wizard — recommended for daily use)
    p_att = sub.add_parser(
        "attend",
        help="Interactive wizard: choose image OCR or paste names directly",
        description=(
            "Guided wizard that asks whether you have a photo or want to paste names,\n"
            "then walks you through course, session, and type selection step by step."
        ),
    )
    p_att.set_defaults(func=cmd_attend)

    # process
    p_proc = sub.add_parser("process", help="Process a single attendance sheet photo")
    p_proc.add_argument("--image", required=True, help="Path to the photo")
    p_proc.add_argument(
        "--course", required=True, choices=["crash", "main", "g10", "g9"],
        help="Target course key"
    )
    p_proc.add_argument("--session", required=True, type=int, help="Session number (1-based)")
    p_proc.add_argument("--type", default=None, dest="type", help="Session type (required for g9/g10)")
    p_proc.add_argument("--dry-run", action="store_true", default=True, help="Dry-run (no writes)")
    p_proc.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Actually write")
    p_proc.add_argument("--force", action="store_true", default=False, help="Overwrite non-empty cells")
    p_proc.set_defaults(func=cmd_process)

    # process-text
    p_proc_txt = sub.add_parser("process-text", help="Process a text file of names")
    p_proc_txt.add_argument("--text", help="Path to text file containing extracted names (prompts if missing)")
    p_proc_txt.add_argument("--course", choices=["crash", "main", "g10", "g9"], help="Target course key (prompts if missing)")
    p_proc_txt.add_argument("--session", type=int, help="Session number (prompts if missing)")
    p_proc_txt.add_argument("--type", default=None, dest="type", help="Session type (prompts if missing)")
    p_proc_txt.add_argument("--dry-run", action="store_true", default=True, help="Dry-run (no writes)")
    p_proc_txt.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Actually write")
    p_proc_txt.add_argument("--force", action="store_true", default=False, help="Overwrite non-empty cells")
    p_proc_txt.add_argument("--yes", "-y", action="store_true", default=False,
                            help="Skip the confirmation prompt when a mark is not recognised")
    p_proc_txt.set_defaults(func=cmd_process_text)

    # review
    p_rev = sub.add_parser("review", help="Manage the review queue")
    rev_sub = p_rev.add_subparsers(dest="review_cmd", required=True)

    p_rev_list = rev_sub.add_parser("list", help="List pending review items")
    p_rev_list.add_argument("--upload-id", default=None)
    p_rev_list.add_argument("--store-dir", default="review_queue")
    p_rev_list.set_defaults(func=cmd_review_list)

    p_rev_res = rev_sub.add_parser("resolve", help="Resolve a flagged item and write it")
    p_rev_res.add_argument("--upload-id", required=True)
    p_rev_res.add_argument("--item-id", required=True)
    p_rev_res.add_argument("--roster-row", required=True, type=int, help="Sheet row to write")
    p_rev_res.add_argument("--roster-name", default=None, help="Student name (for audit log)")
    p_rev_res.add_argument("--value", required=True, type=_attendance_value,
                           help="Present|Late|Not complete|Absent (aliases like P, ✓, A accepted)")
    p_rev_res.add_argument("--type", default=None, dest="session_type", help="Session type (typed_pair courses)")
    p_rev_res.add_argument("--dry-run", action="store_true", default=False)
    p_rev_res.set_defaults(func=cmd_review_resolve)

    p_rev_clean = rev_sub.add_parser("cleanup", help="Delete resolved review images older than retention period")
    p_rev_clean.add_argument("--images-dir", default="review_images", help="Directory containing review images")
    p_rev_clean.set_defaults(func=cmd_review_cleanup)

    # ocr-test
    p_ocr = sub.add_parser("ocr-test", help="Run OCR on a folder of images (no Sheets needed)")
    p_ocr.add_argument("--folder", required=True, help="Path to folder containing images")
    p_ocr.set_defaults(func=cmd_ocr_test)

    # aliases
    p_al = sub.add_parser("aliases", help="Manage the learned-alias store")
    al_sub = p_al.add_subparsers(dest="aliases_cmd", required=True)

    p_al_sug = al_sub.add_parser(
        "suggest",
        help=(
            "Scan learned_aliases.json and print name_variants.yaml candidates. "
            "Run after a few real sessions to surface recurring spelling differences."
        ),
    )
    p_al_sug.add_argument(
        "--alias-path",
        default="config/learned_aliases.json",
        help="Path to the learned aliases JSON file (default: config/learned_aliases.json)",
    )
    p_al_sug.add_argument(
        "--min-confirmations",
        type=int,
        default=2,
        help="Minimum times a paper→roster pair must be confirmed before it is suggested (default: 2)",
    )
    p_al_sug.set_defaults(func=cmd_aliases_suggest)

    def _alias_args(p):
        p.add_argument("--alias-path", default="config/learned_aliases.json",
                       help="Path to the learned aliases JSON file (default: config/learned_aliases.json)")
        p.add_argument("--stale-days", type=int, default=90, help="Flag aliases unused for this many days (default: 90)")

    p_al_list = al_sub.add_parser("list", help="List learned aliases with confirmations, dates and flags")
    _alias_args(p_al_list)
    p_al_list.add_argument("--course", default=None, help="Only this course")
    p_al_list.add_argument("--flagged-only", action="store_true", help="Only old / unused / conflicting aliases")
    p_al_list.set_defaults(func=cmd_aliases_list)

    p_al_rm = al_sub.add_parser("remove", help="Remove a learned alias (a .bak copy is kept)")
    _alias_args(p_al_rm)
    p_al_rm.add_argument("course", help="Course, e.g. g10")
    p_al_rm.add_argument("name", help="Paper name as shown by `aliases list`")
    p_al_rm.add_argument("--roster-name", default=None, help="Remove only the link to this roster name")
    p_al_rm.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation")
    p_al_rm.set_defaults(func=cmd_aliases_remove)

    p_al_ex = al_sub.add_parser("export", help="Export all aliases to a CSV (Excel-friendly) or .json file")
    _alias_args(p_al_ex)
    p_al_ex.add_argument("out", help="Output file, e.g. aliases.csv")
    p_al_ex.set_defaults(func=cmd_aliases_export)

    # aliases variants list|review  (Feature 15: approve suggested name variants before they take effect)
    p_var = al_sub.add_parser("variants", help="Review spelling-variant suggestions learned from reviewer decisions")
    var_sub = p_var.add_subparsers(dest="variants_cmd", required=True)
    for _name, _fn, _help in (("list", cmd_variants_list, "Show pending suggestions (changes nothing)"),
                              ("review", cmd_variants_review, "Approve / reject suggestions one by one")):
        p_v = var_sub.add_parser(_name, help=_help)
        p_v.add_argument("--config", default="config/attendance.yaml")
        p_v.add_argument("--suggestions-file", default=None, help="Default: attendance.match.variant_learning.suggestions_file")
        p_v.add_argument("--variants-file", default=None, help="Default: attendance.match.variants_file")
        p_v.add_argument("--alias-path", default="config/learned_aliases.json")
        p_v.add_argument("--from-aliases", action="store_true", help="Also import suggestions from the learned-alias store")
        p_v.add_argument("--min-count", type=int, default=1, help="Only show suggestions seen at least this many times")
        p_v.set_defaults(func=_fn)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
