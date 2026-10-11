"""attendance/bot/cards.py — pure formatting of a review card (no Telegram objects, no I/O).

``format_review_card(item, upload_id=...)`` returns ``(caption, keyboard_rows)``; each keyboard row
is a list of ``(button_text, callback_data)`` tuples that the sender turns into InlineKeyboardButtons.
callback_data keeps the long-standing format ``res|<upload12>|<item8>|<row|absent|0>`` and
``srch|<upload12>|<item8>``.
"""
from __future__ import annotations

import re
from typing import Optional

# Plain-language reason shown to the reviewer (keys are ReasonCode values).
REASON_TEXT = {
    "MATCH_AMBIGUOUS": "Two students look alike",
    "MATCH_LOW_SCORE": "No close match",
    "OCR_LOW_CONFIDENCE": "Handwriting hard to read",
    "OCR_UNREADABLE": "Couldn't read this line",
    "MATCH_ROSTER_COLLISION": "Already matched to someone else",
}
DEFAULT_REASON_TEXT = "Needs a quick check"

GREEN_AT = 85      # >= 85  -> 🟢
YELLOW_AT = 70     # 70-84  -> 🟡, below -> 🔴


def escape_markdown(text: object) -> str:
    """Escape Telegram legacy-Markdown specials (_ * ` [) so a student's scribble can't break the card."""
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


def traffic_light(pct: float) -> str:
    return "🟢" if pct >= GREEN_AT else "🟡" if pct >= YELLOW_AT else "🔴"


def _pct(value) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def format_review_card(item: dict, *, upload_id: str,
                       position: Optional[tuple[int, int]] = None) -> tuple[str, list[list[tuple[str, str]]]]:
    """Caption (legacy Markdown) and button rows for one pending review item.

    ``position`` is ``(current, total)`` for the "(2 of 5)" heading. The traffic light uses the
    ROUNDED percent shown on the button, so the colour never disagrees with the number.
    """
    up, iid = upload_id[:12], str(item.get("item_id", ""))[:8]

    rows: list[list[tuple[str, str]]] = []
    for match in item.get("top3") or []:
        pct = _pct(match.get("score"))
        rows.append([(f"{traffic_light(pct)} {match.get('roster_name', '?')} ({pct}%)",
                      f"res|{up}|{iid}|{match.get('roster_row')}")])
    rows.append([("❌ Absent", f"res|{up}|{iid}|absent"), ("🔍 Search", f"srch|{up}|{iid}")])
    rows.append([("⏭️ Skip / Ignore", f"res|{up}|{iid}|0")])

    heading = "⚠️ *Review Required*" + (f" ({position[0]} of {position[1]})" if position else "")
    reason = REASON_TEXT.get(str(item.get("reason_code", "")), DEFAULT_REASON_TEXT)
    caption = (
        f"{heading}\n"
        f"Paper name: {escape_markdown(item.get('raw_name', '?'))} (Row {item.get('paper_row_index', '?')})\n"
        f"Why: {reason}\n"
        f"Handwriting confidence: {_pct(float(item.get('ocr_confidence') or 0) * 100)}%"
    )
    return caption, rows
