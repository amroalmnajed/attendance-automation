"""Feature 15 — spelling-variant learning: suggestions are separate, and only human-approved ones take effect."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from attendance import cli, normalizer, variants

HAND = "# hand-written, do not touch\ngiven_name_variants:\n  Gaidaa:\n    - Gidaa   # keep this comment\nprefix_particles:\n  Al: [El]\n"


def _vfile(tmp_path, text=HAND, newline=None):
    p = tmp_path / "name_variants.yaml"
    p.write_bytes((text if newline is None else text.replace("\n", newline)).encode("utf-8"))
    return str(p)


def test_only_real_spelling_variants_are_paired():
    assert variants.token_pairs("Hala Mahmod", "Hala Mahmoud") == []                  # already a known variant
    assert variants.token_pairs("Zeinab Salah", "Zainab Salah") == [("zeinab", "zainab")]
    assert variants.token_pairs("Maryam Ahmed", "Mariam Ayman Bukhari") == []        # Ahmed/Ayman are different names
    assert variants.token_pairs("Sara", "Unknown") == [] and variants.token_pairs("Sara", "IGNORED") == []
    assert variants.token_pairs("Sara Nour", "Sara Nour") == []


def test_record_counts_and_keeps_the_alias_store_out_of_it(tmp_path):
    sug = variants.VariantSuggestions(str(tmp_path / "s.json"))
    assert sug.record("g10", "Zeinab Salah", "Zainab Salah") == 1
    sug.record("g9", "Zeinab Mostafa", "Zainab Mostafa")
    (it,) = sug.pending()
    assert (it["paper_token"], it["roster_token"], it["count"]) == ("zeinab", "zainab", 2)
    assert sorted(it["courses"]) == ["g10", "g9"] and it["status"] == "pending"
    assert not (tmp_path / "learned_aliases.json").exists()


def test_recording_alone_never_changes_matching(tmp_path):
    variants.VariantSuggestions(str(tmp_path / "s.json")).record("g10", "Zeinab Salah", "Zainab Salah")
    assert normalizer.normalize_for_matching("Zeinab") == "zeinab"          # still not folded


def test_approve_edits_only_its_own_section_and_keeps_a_backup(tmp_path):
    vf = _vfile(tmp_path)
    assert variants.approve("zeinab", "zainab", vf) == "added"
    text = Path(vf).read_text(encoding="utf-8")
    assert text.startswith(HAND.rstrip("\n"))                                # hand-written part byte-identical
    assert Path(vf + ".bak").read_text(encoding="utf-8") == HAND
    assert yaml.safe_load(text)["learned_variants"] == {"Zainab": ["Zeinab"]}
    variants.approve("zainabb", "zainab", vf)
    assert yaml.safe_load(Path(vf).read_text(encoding="utf-8"))["learned_variants"] == {"Zainab": ["Zeinab", "Zainabb"]}
    assert Path(vf).read_text(encoding="utf-8").count("learned_variants:") == 1


def test_approve_refuses_conflicts_and_knows_duplicates(tmp_path):
    vf = _vfile(tmp_path)
    assert variants.approve("gidaa", "gaidaa", vf) == "exists"
    assert variants.approve("gidaa", "gada", vf) == "conflict:gaidaa"
    assert "learned_variants" not in Path(vf).read_text(encoding="utf-8")


def test_approved_variant_takes_effect_and_crlf_files_stay_crlf(tmp_path, monkeypatch):
    vf = _vfile(tmp_path, newline="\r\n")
    variants.approve("zeinab", "zainab", vf)
    raw = Path(vf).read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    monkeypatch.setattr(normalizer, "_variants_table", None)               # simulate a bot restart
    monkeypatch.setattr(normalizer, "_phrase_table", [])
    normalizer._load_variants(vf)
    assert normalizer.normalize_for_matching("Zeinab Salah") == normalizer.normalize_for_matching("Zainab Salah")


def _args(tmp_path, **kw):
    return SimpleNamespace(config=str(tmp_path / "none.yaml"), suggestions_file=str(tmp_path / "s.json"),
                           variants_file=_vfile(tmp_path), alias_path=str(tmp_path / "al.json"),
                           from_aliases=False, min_count=1, **kw)


def test_cli_review_approve_reject_and_list_changes_nothing(tmp_path, monkeypatch, capsys):
    a = _args(tmp_path)
    sug = variants.VariantSuggestions(a.suggestions_file)
    sug.record("g10", "Zeinab Salah", "Zainab Salah")
    sug.record("g10", "Mona Adel", "Mina Adel")                              # similar but a different person
    cli.cmd_variants_list(a)
    assert "Nothing has been applied" in capsys.readouterr().out
    assert "learned_variants" not in Path(a.variants_file).read_text(encoding="utf-8")
    answers = iter(["a", "r"])                                               # order: by count then token -> mona first
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    cli.cmd_variants_review(a)
    learned = yaml.safe_load(Path(a.variants_file).read_text(encoding="utf-8")).get("learned_variants", {})
    assert learned == {"Mina": ["Mona"]}                                     # approved; Zeinab was rejected
    status = {i["paper_token"]: i["status"] for i in json.loads(Path(a.suggestions_file).read_text(encoding="utf-8"))["items"]}
    assert status == {"mona": "approved", "zeinab": "rejected"}
    assert sug.pending() == []                                               # both decided
    sug.record("g10", "Mona Adel", "Mina Adel")                              # a rejected pair never comes back
    assert all(i["status"] != "pending" for i in json.loads(Path(a.suggestions_file).read_text(encoding="utf-8"))["items"])


def test_cli_can_seed_suggestions_from_the_alias_store(tmp_path, capsys):
    a = _args(tmp_path)
    Path(a.alias_path).write_text(json.dumps({"g10": {"zeinab salah": {"Zainab Salah": 3}, "maryam ahmed": {"Mariam Ayman": 1}}}), encoding="utf-8")
    a.from_aliases = True
    cli.cmd_variants_list(a)
    out = capsys.readouterr().out
    assert "zeinab" in out and "ahmed" not in out


def test_record_resolution_is_safe_with_fake_or_disabled_config(tmp_path):
    fake = {"attendance": {"review": {}, "match": {"variant_learning": {"suggestions_file": str(tmp_path / "f.json")}}}}
    assert variants.record_resolution(fake, "g10", "Zeinab Salah", "Zainab Salah") == 1   # tiny fake config works
    cfg = {"attendance": {"match": {"variant_learning": {"enabled": False, "suggestions_file": str(tmp_path / "s.json")}}}}
    assert variants.record_resolution(cfg, "g10", "Zeinab Salah", "Zainab Salah") == 0
    assert not (tmp_path / "s.json").exists()
