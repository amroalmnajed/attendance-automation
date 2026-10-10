"""tests/test_normalizer.py - Unit tests for normalizer.py.

Covers:
 - Latin normalisation (lowercase, accents, collapse)
 - normalize_for_matching (the combined entry point)
 - Attendance mark mapping
"""
import pytest
from attendance.normalizer import (
    normalize_latin,
    normalize_for_matching,
    normalize_attendance_mark,
)

# — Latin normalisation —

class TestNormalizeLatin:
    def test_lowercase(self):
        assert normalize_latin("Mohamed Ali") == "mohamed ali"

    def test_accent_strip(self):
        assert normalize_latin("Ünsal") == "unsal"

    def test_punctuation_strip(self):
        # El-Dibany collapses to "eldibany" (El-prefix collapse) — same as Eldibany
        assert normalize_latin("El-Dibany") == "eldibany"

    def test_el_prefix_hyphen_collapse(self):
        # El-X collapses to ElX so hyphenated and fused forms tokenize identically
        assert normalize_latin("El-Sharkawy") == "elsharkawy"
        assert normalize_latin("Elsharkawy") == "elsharkawy"
        assert normalize_latin("Al-Sayed") == "alsayed"

    def test_collapse_spaces(self):
        assert normalize_latin("  Mohamed   Ali  ") == "mohamed ali"


# — Combined normalize_for_matching —

class TestNormalizeForMatching:
    def test_latin_input_lowercased(self):
        assert normalize_for_matching("Mohamed Ali") == "mohamed ali"

    def test_el_dibany_variant(self):
        r1 = normalize_for_matching("El Dibany")
        r2 = normalize_for_matching("Eldibany")
        assert r1.replace(" ", "") == r2.replace(" ", "")

    def test_el_sharkawy_variant(self):
        # "El-Sharkawy" on paper vs "Elsharkawy" in roster — must produce same token
        r1 = normalize_for_matching("El-Sharkawy")
        r2 = normalize_for_matching("Elsharkawy")
        assert r1 == r2

    def test_moustafa_mostafa_variant(self):
        # "Moustafa" and "Mostafa" must fold to the same canonical form
        r1 = normalize_for_matching("Moustafa")
        r2 = normalize_for_matching("Mostafa")
        assert r1 == r2

    def test_shereen_shireen_variant(self):
        r1 = normalize_for_matching("Shereen")
        r2 = normalize_for_matching("Shireen")
        assert r1 == r2


# — Attendance mark mapping —

class TestNormalizeAttendanceMark:
    def test_present_english(self):
        assert normalize_attendance_mark("P") == "Present"
        assert normalize_attendance_mark("present") == "Present"
        assert normalize_attendance_mark("✓") == "Present"

    def test_absent_english(self):
        assert normalize_attendance_mark("A") == "Absent"
        assert normalize_attendance_mark("absent") == "Absent"

    def test_late_english(self):
        assert normalize_attendance_mark("L") == "Late"
        assert normalize_attendance_mark("late") == "Late"

    def test_not_complete_english(self):
        assert normalize_attendance_mark("NC") == "Not complete"
        assert normalize_attendance_mark("not complete") == "Not complete"

    def test_none_returns_none(self):
        assert normalize_attendance_mark(None) is None
        assert normalize_attendance_mark("") is None

    def test_free_maps_to_not_complete(self):
        assert normalize_attendance_mark("Free") == "Not complete"

    def test_unknown_returns_none(self):
        assert normalize_attendance_mark("XYZ_UNKNOWN") is None


def test_phrase_variants_fold():
    assert normalize_for_matching("Abdel Aziz Ahmed") == normalize_for_matching("Abdelaziz Ahmed")
    assert normalize_for_matching("Abd El Rahman") == normalize_for_matching("Abdel Rahman")
    assert normalize_for_matching("Farida El Sharkawy") == normalize_for_matching("Farida Elsharkawy")


def test_eldin_not_corrupted():
    assert normalize_for_matching("Salah Eldin") == "salah eldin"


# — Expanded _AR_TO_LATIN coverage —

class TestArabicTransliteration:
    def test_tamer_transliterated(self):
        """تامر should map to 'tamer'."""
        result = normalize_for_matching("تامر فؤاد")
        assert "tamer" in result

    def test_nour_transliterated(self):
        assert "nour" in normalize_for_matching("نور")

    def test_ziad_transliterated(self):
        assert "ziad" in normalize_for_matching("زياد")

    def test_saber_transliterated(self):
        assert "saber" in normalize_for_matching("صابر")

    def test_nasser_transliterated(self):
        assert "nasser" in normalize_for_matching("ناصر")

    def test_sara_transliterated(self):
        assert "sara" in normalize_for_matching("سارة")

    def test_ajami_transliterated(self):
        """عجمي (Ajami family name) should transliterate to 'ajami'."""
        assert "ajami" in normalize_for_matching("عجمي")


# — suggest_variants_from_aliases —

import json
import os
import tempfile


def test_suggest_variants_returns_empty_when_no_alias_file():
    from attendance.normalizer import suggest_variants_from_aliases
    result = suggest_variants_from_aliases(
        alias_path="/nonexistent/path/learned_aliases.json",
        min_confirmations=1,
    )
    assert result == []


def test_suggest_variants_returns_suggestions():
    from attendance.normalizer import suggest_variants_from_aliases

    alias_data = {
        "crash": {
            # normalized("Mohmad Ali") → "mohamed ali" (paper has typo)
            "mohmad ali": {"Mohamed Ali Salem": 3},
        }
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(alias_data, f, ensure_ascii=False)
        tmp_path = f.name

    try:
        suggestions = suggest_variants_from_aliases(
            alias_path=tmp_path, min_confirmations=1
        )
        # "mohmad" ≠ "mohamed" — should appear as a suggestion
        paper_tokens = [s["paper_token"] for s in suggestions]
        assert "mohmad" in paper_tokens
        roster_tokens = [s["roster_token"] for s in suggestions]
        assert "mohamed" in roster_tokens
    finally:
        os.unlink(tmp_path)


def test_suggest_variants_deduplicates():
    from attendance.normalizer import suggest_variants_from_aliases

    alias_data = {
        "crash": {
            "mohmad ali": {"Mohamed Ali Salem": 3},
        },
        "main": {
            "mohmad khaled": {"Mohamed Khaled Hassan": 5},
        },
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(alias_data, f, ensure_ascii=False)
        tmp_path = f.name

    try:
        suggestions = suggest_variants_from_aliases(
            alias_path=tmp_path, min_confirmations=1
        )
        # Both courses have "mohmad" → "mohamed"; should appear once (deduped)
        paper_tokens = [s["paper_token"] for s in suggestions]
        assert paper_tokens.count("mohmad") == 1
        # The surviving entry should have the higher confirmation count (5)
        mohmad_suggestion = next(s for s in suggestions if s["paper_token"] == "mohmad")
        assert mohmad_suggestion["confirmations"] == 5
    finally:
        os.unlink(tmp_path)
