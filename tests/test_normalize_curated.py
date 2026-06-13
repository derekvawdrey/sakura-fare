"""Unit tests for the curated-fare fallback normalization."""
from app.agent.tools import _normalize_curated


def test_published_route():
    curated = {
        "ok": True, "basis": "published",
        "from": {"id": "tokyo", "name": "Tokyo"}, "to": {"id": "kyoto", "name": "Kyoto"},
        "options": [{"line": "Tokaido Shinkansen", "train": "Nozomi", "fare_jpy": 14170}],
        "note": "Published fares.",
    }
    r = _normalize_curated(curated, "Tokyo", "Kyoto")
    assert r is not None
    assert r["fare_jpy"] == 14170
    assert r["basis"] == "published"
    assert "Tokaido Shinkansen" in r["route"]
    assert r["fare_text"] == "¥14,170"


def test_estimated_route_with_duration():
    curated = {
        "ok": True, "basis": "estimated",
        "from": {"name": "A"}, "to": {"name": "B"},
        "options": [{"line": "Estimated rail route", "train": "Local", "fare_jpy": 800,
                     "duration_min": 30, "source": "est"}],
        "note": "estimate",
    }
    r = _normalize_curated(curated, "A", "B")
    assert r is not None
    assert r["fare_jpy"] == 800
    assert r["basis"] == "estimated"
    assert r["duration"] == "30 min"


def test_unresolved_or_fareless_returns_none():
    assert _normalize_curated({"ok": False, "error": "unknown"}, "X", "Y") is None
    assert _normalize_curated({"ok": True, "options": []}, "X", "Y") is None
    assert _normalize_curated({"ok": True, "options": [{"line": "x"}]}, "X", "Y") is None
