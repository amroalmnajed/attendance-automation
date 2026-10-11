"""T3 - roster names are normalised once per matcher; results unchanged."""
from attendance import matcher as matcher_mod
from attendance.matcher import Matcher

ROSTER = [("Ahmed Hassan Ali", 5), ("Mona Said Ibrahim", 6), ("Omar Khaled Samy", 7)]


def _count_calls(monkeypatch):
    calls = []
    real = matcher_mod.normalize_for_matching
    monkeypatch.setattr(matcher_mod, "normalize_for_matching", lambda t: calls.append(t) or real(t))
    return calls


def test_each_roster_name_is_normalised_once_across_queries(monkeypatch):
    calls = _count_calls(monkeypatch)
    m = Matcher()
    for i, q in enumerate(["Ahmed Hasan Ali", "Mona Said", "Omar Khaled"]):
        m.match(q, 0.95, ROSTER, i)
    roster_calls = [c for c in calls if c in dict(ROSTER)]
    assert sorted(roster_calls) == sorted(n for n, _ in ROSTER)      # exactly once each


def test_cached_results_equal_uncached(monkeypatch):
    queries = ["Ahmed Hassan Ali", "ahmad hasan", "Mona Said Ibrahim", "Omar", "zzz qqq"]

    def run(m):
        return [m.match(q, 0.95, ROSTER, i) for i, q in enumerate(queries)]
    cached = run(Matcher())
    plain = Matcher()
    monkeypatch.setattr(plain, "_norm", matcher_mod.normalize_for_matching)    # bypass the cache
    for a, b in zip(cached, run(plain)):
        assert (a.decision, a.reason, a.normalized_query) == (b.decision, b.reason, b.normalized_query)
        assert [(c.roster_name, c.score) for c in a.top3] == [(c.roster_name, c.score) for c in b.top3]


def test_ocr_hint_and_alias_paths_use_the_cache(monkeypatch):
    calls = _count_calls(monkeypatch)
    m = Matcher(alias_lookup=lambda raw: "Mona Said Ibrahim" if raw == "mm" else None)
    m.match("mm", 0.95, ROSTER, 1)                                   # alias path
    m.match("Omar K", 0.95, ROSTER, 2, ocr_suggested_name="Omar Khaled Samy",
            ocr_suggested_confidence=0.95)                           # OCR-hint path
    per_name = {n: sum(1 for c in calls if c == n) for n, _ in ROSTER}
    assert all(v <= 1 for v in per_name.values()), per_name


def test_reset_clears_cache_and_claims():
    m = Matcher()
    m.match("Ahmed Hassan Ali", 0.95, ROSTER, 1)
    assert m._norm_cache and m._claimed
    m.reset()
    assert not m._norm_cache and not m._claimed


def test_cache_is_bounded():
    m = Matcher()
    for i in range(5100):
        m._norm(f"name {i}")
    assert len(m._norm_cache) <= 5000
