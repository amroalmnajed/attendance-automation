"""T7 - review card: traffic lights, plain-language reason, escaped name, unchanged callback_data."""
import pytest

from attendance.review_cards import REASON_TEXT, escape_markdown, format_review_card, traffic_light

UP = "abcdef0123456789abcd"          # 20 chars; callbacks use the first 12


def _item(**kw):
    base = {"item_id": "i1234567890", "raw_name": "Sara M", "paper_row_index": 4, "ocr_confidence": 0.82,
            "reason_code": "MATCH_AMBIGUOUS",
            "top3": [{"roster_name": "Sara Mohamed", "roster_row": 7, "score": 91.2},
                     {"roster_name": "Sarah Morsi", "roster_row": 9, "score": 76.0},
                     {"roster_name": "Salma Nabil", "roster_row": 12, "score": 41.0}]}
    base.update(kw)
    return base


@pytest.mark.parametrize("pct,light", [(100, "🟢"), (85, "🟢"), (84, "🟡"), (70, "🟡"), (69, "🔴"), (0, "🔴")])
def test_traffic_light_thresholds(pct, light):
    assert traffic_light(pct) == light


def test_buttons_put_the_name_first_with_light_and_percent():
    _, rows = format_review_card(_item(), upload_id=UP)
    assert [r[0][0] for r in rows[:3]] == ["🟢 Sara Mohamed (91%)", "🟡 Sarah Morsi (76%)", "🔴 Salma Nabil (41%)"]


def test_light_follows_the_rounded_percent_shown():
    # 84.6 is shown as 85% -> green, so colour and number never disagree
    _, rows = format_review_card(_item(top3=[{"roster_name": "A B", "roster_row": 3, "score": 84.6},
                                             {"roster_name": "C D", "roster_row": 4, "score": 69.4}]), upload_id=UP)
    assert rows[0][0][0] == "🟢 A B (85%)" and rows[1][0][0] == "🔴 C D (69%)"


def test_callback_data_format_is_unchanged():
    _, rows = format_review_card(_item(), upload_id=UP)
    up, iid = UP[:12], "i1234567"
    assert [r[0][1] for r in rows[:3]] == [f"res|{up}|{iid}|7", f"res|{up}|{iid}|9", f"res|{up}|{iid}|12"]
    assert rows[3] == [("❌ Absent", f"res|{up}|{iid}|absent"), ("🔍 Search", f"srch|{up}|{iid}")]
    assert rows[4] == [("⏭️ Skip / Ignore", f"res|{up}|{iid}|0")]
    assert all(len(d.encode()) <= 64 for row in rows for _, d in row)       # Telegram limit


@pytest.mark.parametrize("code,text", list(REASON_TEXT.items()))
def test_each_reason_has_its_plain_sentence(code, text):
    caption, _ = format_review_card(_item(reason_code=code), upload_id=UP)
    assert f"Why: {text}" in caption


def test_spec_wording_of_the_five_reasons():
    assert REASON_TEXT == {"MATCH_AMBIGUOUS": "Two students look alike", "MATCH_LOW_SCORE": "No close match",
                           "OCR_LOW_CONFIDENCE": "Handwriting hard to read", "OCR_UNREADABLE": "Couldn't read this line",
                           "MATCH_ROSTER_COLLISION": "Already matched to someone else"}


@pytest.mark.parametrize("code", ["MARK_UNRECOGNIZED", "SOMETHING_NEW", "", None])
def test_unknown_or_missing_reason_gets_a_generic_line(code):
    caption, _ = format_review_card(_item(reason_code=code), upload_id=UP)
    assert "Why: Needs a quick check" in caption


def test_name_is_escaped_so_markdown_cannot_break():
    caption, _ = format_review_card(_item(raw_name="a_b*c`d[e"), upload_id=UP)
    assert "a\\_b\\*c\\`d\\[e" in caption and escape_markdown("x_y") == "x\\_y"


def test_heading_shows_position_and_confidence():
    caption, _ = format_review_card(_item(), upload_id=UP, position=(2, 5))
    assert "Review Required* (2 of 5)" in caption and "Row 4" in caption and "82%" in caption


def test_missing_fields_do_not_crash():
    caption, rows = format_review_card({"item_id": "x"}, upload_id=UP)
    assert "Why:" in caption and len(rows) == 2                        # no top3 -> only the 2 action rows
