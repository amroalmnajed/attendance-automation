"""webapp/pages/admin.py — Admin tools (W7).

Screens for revert, absent-offer, alias management, and health status.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st
import yaml

from attendance.session_resolver import resolve as session_resolve
from attendance.writer import WriteCell, batch_write
from webapp.config import CONFIG_PATH, COURSES


def _load_config() -> dict:
    """Load the attendance config."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _page_admin() -> None:
    """Render the admin tools screen."""
    st.header("🛠️ Admin Tools")

    # Tabs for each admin tool
    tab1, tab2, tab3, tab4 = st.tabs(["Revert", "Absent Offer", "Aliases", "Health"])

    with tab1:
        _render_revert()

    with tab2:
        _render_absent_offer()

    with tab3:
        _render_aliases()

    with tab4:
        _render_health()


def _render_revert() -> None:
    """Render the revert tool."""
    st.subheader("🔄 Revert Previous Upload")
    st.markdown("Revert all cells modified by a specific upload ID to their previous values.")

    from attendance.reporter import read_audit_log, plan_revert

    cfg = _load_config()
    records = read_audit_log()

    # Get unique non-dry-run upload IDs from audit log
    recent_uploads = []
    seen = set()
    for r in reversed(records):
        if not r.dry_run and r.upload_id not in seen:
            seen.add(r.upload_id)
            recent_uploads.append(f"{r.upload_id} ({r.course} | {r.ts[:16]})")

    selected_choice = st.selectbox(
        "Select recent upload or enter ID below",
        options=["-- Manual input --"] + recent_uploads[:20],
    )

    if selected_choice != "-- Manual input --":
        target_upload_id = selected_choice.split()[0]
    else:
        target_upload_id = st.text_input("Upload ID to revert", placeholder="e.g. 7f8a9b0c...").strip()

    if not target_upload_id:
        st.info("Select or enter an upload ID to view the revert plan.")
        return

    plan = plan_revert(target_upload_id, records)
    if not plan:
        st.warning(f"No revertible cells found in audit log for upload ID: `{target_upload_id}`")
        return

    st.subheader(f"Plan for upload `{target_upload_id}` ({len(plan)} cells)")

    table_data = []
    has_overwritten = False
    for p in plan:
        status = "⚠️ Overwritten since" if p.overwritten_since else "OK"
        if p.overwritten_since:
            has_overwritten = True
        table_data.append({
            "Cell": p.cell,
            "Sheet Row": p.sheet_row,
            "Col": p.col,
            "Current In Audit": p.current_in_audit or "(empty)",
            "Revert To": p.revert_to_value or "(empty)",
            "Status": status,
        })

    st.dataframe(table_data, use_container_width=True)

    if has_overwritten:
        st.warning("⚠️ Some cells have been overwritten by newer uploads. Reverting will overwrite their current values.")

    dry_run = st.toggle("Dry run (preview only)", value=True, key="revert_dry_run")

    if st.button("Apply Revert", type="primary"):
        # Find course & sheet_id from matching audit record
        matching_record = next((r for r in records if r.upload_id == target_upload_id), None)
        if not matching_record:
            st.error("Could not find course information for this upload.")
            return

        course = matching_record.course
        cells_to_write = [
            WriteCell(
                roster_name=f"Row {p.sheet_row}",
                sheet_row=p.sheet_row,
                col=p.col,
                old_value=p.current_in_audit,
                new_value=p.revert_to_value or "",
            )
            for p in plan
        ]

        with st.spinner("Applying revert…"):
            try:
                # Find matching session
                session_num = 1
                try:
                    import re
                    m = re.search(r"\d+", matching_record.cell)
                except Exception:
                    pass

                session_info = session_resolve(course_key=course, session_num=session_num, cfg=cfg)
                retry_cfg = cfg.get("attendance", {}).get("write", {}).get("retry", {})
                user = st.session_state.get("user_name", "webapp-admin")

                res = batch_write(
                    session=session_info,
                    cells=cells_to_write,
                    upload_id=f"rev_{target_upload_id[:8]}",
                    dry_run=dry_run,
                    retry_cfg=retry_cfg,
                )

                if dry_run:
                    st.success(f"🧪 [DRY-RUN] Would revert {len(cells_to_write)} cell(s).")
                else:
                    st.success(f"✅ Reverted {len(cells_to_write)} cell(s) successfully.")
            except Exception as exc:
                st.error(f"Revert failed: {str(exc)}")


def _render_absent_offer() -> None:
    """Render the absent-marking offer tool."""
    st.subheader("📢 Mark Missing Students Absent")
    st.markdown("After an upload, students on the roster who were not matched can be marked absent in bulk.")

    from attendance.absent import load_pending, mark_missing_absent

    cfg = _load_config()
    review_dir = cfg.get("attendance", {}).get("review", {}).get("local_dir", "review_queue")
    absent_store = Path(review_dir) / "_absent"

    pending_files = list(absent_store.glob("*.json")) if absent_store.exists() else []

    if not pending_files:
        st.info("No pending absent offers found. (Absent offers are recorded after photo or Zoom uploads for eligible courses).")
        return

    options = []
    for f in pending_files:
        rec = load_pending(review_dir, f.stem)
        if rec:
            options.append(f"{rec['upload_id']} — {rec['course'].upper()} S{rec['session']} ({rec.get('created_at', '')[:16]})")

    selected = st.selectbox("Select pending upload offer", options=options)
    if not selected:
        return

    sel_upload_id = selected.split()[0]
    rec = load_pending(review_dir, sel_upload_id)

    if rec:
        col1, col2, col3 = st.columns(3)
        col1.metric("Course", rec["course"].upper())
        col2.metric("Session", rec["session"])
        col3.metric("Matched Students", len(rec.get("matched_rows", [])))

        if rec.get("dry_run"):
            st.info("ℹ️ This upload was run in dry-run mode. Marking absent will also preview only.")

        if st.button("Mark Missing Absent", type="primary"):
            with st.spinner("Writing absent marks…"):
                res = mark_missing_absent(sel_upload_id, cfg=cfg, review_dir=review_dir)
                if res.ok:
                    tag = "🧪 [DRY-RUN] " if res.dry_run else "✅ "
                    st.success(f"{tag}Marked {res.marked} student(s) absent.")
                else:
                    st.error(f"Could not mark absent: {res.reason}")


def _render_aliases() -> None:
    """Render the alias management tool."""
    st.subheader("📚 Learned Aliases Management")

    from attendance.aliases import store_from_cfg

    cfg = _load_config()
    store = store_from_cfg(cfg)

    course_keys = list(COURSES.keys())
    selected_course = st.selectbox("Filter by course", options=["All Courses"] + course_keys)

    entries = store.entries(stale_days=90)
    if selected_course != "All Courses":
        entries = [e for e in entries if e["course"].lower() == selected_course.lower()]

    if not entries:
        st.info("No learned aliases found for this course.")
    else:
        st.markdown(f"Found **{len(entries)}** alias rule(s):")
        table = [
            {
                "Course": e["course"].upper(),
                "Paper Name": e["key"],
                "Roster Name": e["roster_name"],
                "Confirmations": e["confirmations"],
                "Last Used": e.get("last_used") or e.get("last_confirmed") or "—",
                "Flags": ", ".join(e.get("flags", [])) or "—",
            }
            for e in entries
        ]
        st.dataframe(table, use_container_width=True)

        st.divider()
        st.subheader("Remove an Alias")
        col1, col2 = st.columns([1, 2])
        with col1:
            remove_course = st.selectbox("Course", options=course_keys, key="rm_alias_course")
        with col2:
            remove_name = st.text_input("Paper name to remove", placeholder="Exact paper name...")

        if st.button("Remove Alias", type="secondary"):
            if remove_name.strip():
                n = store.remove(remove_course.lower(), remove_name.strip())
                if n:
                    st.success(f"Removed alias for '{remove_name}' in {remove_course}.")
                    st.rerun()
                else:
                    st.warning(f"No alias found matching '{remove_name}'.")
            else:
                st.error("Please enter a paper name to remove.")


def _render_health() -> None:
    """Render the health status tool."""
    st.subheader("🏥 System Health Status")

    from attendance.health import status_text

    cfg = _load_config()

    if st.button("🔄 Refresh Health Status"):
        st.rerun()

    status = status_text(cfg)
    st.code(status, language="markdown")


# Maintain backward compatibility with unit tests expecting stub function names
_render_revert_stub = _render_revert
_render_absent_offer_stub = _render_absent_offer
_render_aliases_stub = _render_aliases
_render_health_stub = _render_health


if __name__ == "__main__":
    _page_admin()
