"""Feature 10 — merging OCR rows from several photos of one sheet."""
from __future__ import annotations

from attendance.ocr import OcrRow
from attendance.photos import merge_photo_rows


def row(name, mark="P", conf=0.9, idx=1):
    return OcrRow(raw_name=name, status_mark=mark, confidence=conf, row_index=idx)


def merge(*photos):
    return merge_photo_rows(list(photos), [f"p{i}.jpg" for i in range(1, len(photos) + 1)])


def test_distinct_names_from_all_photos_are_kept_in_order():
    out = merge([row("Ahmed Ali"), row("Sara Hassan", idx=2)], [row("Omar Youssef")])
    assert [r.raw_name for r in out] == ["Ahmed Ali", "Sara Hassan", "Omar Youssef"]


def test_every_row_remembers_its_photo():
    out = merge([row("Ahmed Ali")], [row("Omar Youssef")])
    assert [(r.photo_index, r.source_image) for r in out] == [(1, "p1.jpg"), (2, "p2.jpg")]


def test_same_name_same_mark_is_merged_into_one_row():
    out = merge([row("Ahmed Ali", "P", 0.80)], [row("ahmed  ali", "present", 0.95)])
    assert len(out) == 1
    assert out[0].conflict_marks is None
    assert out[0].photo_index == 2                      # the better-read row wins


def test_blank_mark_on_one_photo_takes_the_mark_from_the_other():
    out = merge([row("Ahmed Ali", None)], [row("Ahmed Ali", "L")])
    assert len(out) == 1 and out[0].status_mark == "L" and out[0].conflict_marks is None


def test_conflicting_marks_are_kept_once_and_tagged_for_review():
    out = merge([row("Ahmed Ali", "P")], [row("Ahmed Ali", "A")])
    assert len(out) == 1
    assert out[0].conflict_marks == ["Present", "Absent"]


def test_third_photo_adds_to_an_existing_conflict_without_duplicating():
    out = merge([row("Ahmed Ali", "P")], [row("Ahmed Ali", "A")], [row("Ahmed Ali", "L")], [row("Ahmed Ali", "A")])
    assert len(out) == 1
    assert out[0].conflict_marks == ["Present", "Absent", "Late"]


def test_unreadable_rows_are_never_merged():
    out = merge([row("[UNREADABLE]", None, 0.2)], [row("[UNREADABLE]", None, 0.2)])
    assert len(out) == 2


def test_two_students_with_the_same_name_on_one_photo_are_not_merged():
    out = merge([row("Ahmed", "P", idx=1), row("Ahmed", "A", idx=2)])
    assert len(out) == 2


def test_ambiguous_repeat_across_photos_is_left_for_the_matcher():
    # Photo 1 has two "Ahmed" rows; photo 2 has one. We cannot know which, so keep all three.
    out = merge([row("Ahmed", "P", idx=1), row("Ahmed", "P", idx=2)], [row("Ahmed", "P")])
    assert len(out) == 3 and all(r.conflict_marks is None for r in out)


def test_single_photo_passes_through_unchanged_apart_from_tags():
    rows = [row("Ahmed Ali"), row("Sara Hassan", idx=2)]
    out = merge(rows)
    assert [r.raw_name for r in out] == ["Ahmed Ali", "Sara Hassan"]
    assert all(r.photo_index == 1 for r in out)


def test_length_mismatch_is_rejected():
    import pytest
    with pytest.raises(ValueError):
        merge_photo_rows([[row("A")]], ["p1.jpg", "p2.jpg"])
