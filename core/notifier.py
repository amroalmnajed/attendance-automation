"""core/notifier.py — Summary notification after each workflow run.

Channels: log (always active), email (via SMTP env vars), telegram (via bot token env var).
Student names are never included in notifications.
"""
from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from typing import Optional

from core.logger import get_logger

log = get_logger("notifier")


@dataclass
class RunSummary:
    upload_id: str
    course: str
    session: str
    matched: int = 0
    written: int = 0
    flagged: int = 0
    unmatched: int = 0
    errors: list[str] = field(default_factory=list)
    review_url: Optional[str] = None
    sheet_url: Optional[str] = None
    dry_run: bool = False
    # "Mark missing students absent" offer (crash/main only). Counts only: student
    # names must never be stored on the summary because send() logs the whole object.
    absent_offer: bool = False
    missing_count: int = 0
    missing_held_back: int = 0
    photo_count: int = 1
    # Feature 6: per-session counts (numbers only) + last session's counts for the same course.
    present_count: int = 0
    late_count: int = 0
    absent_count: int = 0
    prev_counts: Optional[dict] = None
    prev_session: str = ""
    drop_warning: str = ""
    # Zoom-specific flags
    teacher_not_found: bool = False
    session_type_ambiguous: bool = False

    def text(self) -> str:
        # Polished Telegram-friendly card format
        tag = "🧪 [DRY-RUN] " if self.dry_run else "✅ "
        lines = [
            f"{tag}*Attendance Recorded!*",
            f"───────────────────────────────",
            f"🏫 *Course:*     {self.course.upper()}",
            f"📅 *Session:*    {self.session}",
            f"⚡ *Auto-match:* {self.matched}",
            f"📝 *Written:*    {self.written}",
            f"⚠️ *Flagged:*    {self.flagged}",
        ]
        if self.unmatched > 0:
            lines.append(f"❓ *Unmatched:*  {self.unmatched}")
        if (self.present_count + self.late_count + self.absent_count) > 0:
            lines.append(
                f"📊 *This session:* Present {self.present_count} · Late {self.late_count}"
                f" · Absent {self.absent_count} · Review {self.flagged}"
            )
            pc = self.prev_counts if isinstance(self.prev_counts, dict) else None
            if pc:
                lines.append(
                    f"↩️ *Previous (S{self.prev_session}):* Present {pc.get('present', 0)}"
                    f" · Late {pc.get('late', 0)} · Absent {pc.get('absent', 0)}"
                )
            if self.drop_warning:
                lines.append(f"📉 *{self.drop_warning}*")
        if self.photo_count > 1:
            lines.append(f"📷 *Photos:*     {self.photo_count} (merged)")
        if self.absent_offer and self.missing_count > 0:
            held = f" ({self.missing_held_back} held back — possible matches in review)" if self.missing_held_back else ""
            lines.append(f"🙈 *Not on sheet:* {self.missing_count}{held}")
        if self.teacher_not_found:
            lines.append(f"⚠️ *Teacher not found in participant list*")
        if self.session_type_ambiguous:
            lines.append(f"⚠️ *Session type ambiguous - manual selection required*")

        if self.errors:
            lines.append(f"\n❌ *Errors:*")
            for e in self.errors[:3]:
                # escape markdown reserved chars for the error string
                e = e.replace("*", "").replace("_", "").replace("[", "").replace("]", "")
                lines.append(f"• {e}")

        return "\n".join(lines)


def send(summary: RunSummary, channel: str = "log") -> None:
    """Dispatch the run summary to the configured notification channel."""
    log.info("run_summary", extra={"summary": summary.__dict__})

    if channel == "email":
        _send_email(summary)
    elif channel == "telegram":
        _send_telegram(summary)
    # "log" channel is always handled above; nothing more to do.


def _send_email(summary: RunSummary) -> None:
    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    recipients_raw = os.getenv("NOTIFY_TO", "")

    if not (smtp_host and smtp_user and recipients_raw):
        log.warning("email_notify_skipped", extra={"reason": "SMTP env vars not set"})
        return

    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    msg = MIMEText(summary.text(), "plain", "utf-8")
    msg["Subject"] = f"[Attendance] {summary.course} S{summary.session}"
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, recipients, msg.as_string())
        log.info("email_sent", extra={"recipients": recipients})
    except Exception as exc:
        log.error("email_failed", extra={"error": str(exc)})


def _send_telegram(summary: RunSummary) -> None:
    import urllib.request, urllib.parse

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not (token and chat_id):
        log.warning("telegram_notify_skipped", extra={"reason": "TELEGRAM env vars not set"})
        return

    text = summary.text()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:
            log.info("telegram_sent", extra={"status": resp.status})
    except Exception as exc:
        log.error("telegram_failed", extra={"error": str(exc)})


def send_text(text: str, chat_ids) -> int:
    """Plain-text Telegram message to specific chats (reminders and health alerts).

    Returns how many chats received it. Never raises. The caller must not put student names in
    ``text`` (counts and course/session labels only); the text itself is not logged.
    """
    import urllib.parse
    import urllib.request

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    ids = [c for c in dict.fromkeys(chat_ids or [])]
    if not (token and ids):
        log.warning("text_notify_skipped", extra={"reason": "no bot token or no recipients"})
        return 0
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    sent = 0
    for chat_id in ids:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": str(text)[:3900]}).encode()
        try:
            with urllib.request.urlopen(url, data=data, timeout=10):
                sent += 1
        except Exception as exc:   # token is in the URL: log the error type only
            log.error("text_notify_failed", extra={"error": type(exc).__name__})
    return sent
