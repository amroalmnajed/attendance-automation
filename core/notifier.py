"""core/notifier.py — Summary notification after each pipeline run.

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
