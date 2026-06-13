"""Unit tests for the gmaps-fares parsing helpers (no Docker/LLM needed)."""
from app.services.gmaps_fares import _csv_rows, _match_row, _yen_to_int


def test_yen_to_int():
    assert _yen_to_int("¥1,290") == 1290
    assert _yen_to_int("¥210") == 210
    assert _yen_to_int("¥19,760") == 19760
    assert _yen_to_int("") is None
    assert _yen_to_int("no fare shown") is None


def test_csv_rows_parses_quoted_fare_with_comma():
    out = (
        "origin,destination,travel_mode,fare,duration,route_summary,url,note\n"
        '"Kanazawa Station",Toyama Station,transit,"¥1,290",1 hr 4 min,IR Ishikawa Line,https://x,\n'
    )
    rows = _csv_rows(out)
    assert len(rows) == 1
    assert rows[0]["origin"] == "Kanazawa Station"
    assert rows[0]["fare"] == "¥1,290"


def test_csv_rows_skips_noise_before_header_and_reads_many():
    out = (
        "some stray log line\n"
        "origin,destination,travel_mode,fare,duration,route_summary,url,note\n"
        "Tokyo Station,Shibuya Station,transit,¥210,18 min,Yamanote,https://a,\n"
        "Kyoto Station,Osaka Station,transit,¥580,28 min,Tokaido,https://b,\n"
    )
    rows = _csv_rows(out)
    assert len(rows) == 2
    assert rows[1]["destination"] == "Osaka Station"


def test_csv_rows_empty_when_no_header():
    assert _csv_rows("just logs\nmore logs\n") == []


def test_match_row_prefers_exact_pair_then_falls_back():
    rows = [
        {"origin": "A", "destination": "B", "fare": "¥100"},
        {"origin": "C", "destination": "D", "fare": "¥200"},
    ]
    assert _match_row(rows, "C", "D")["fare"] == "¥200"
    assert _match_row(rows, "X", "Y")["fare"] == "¥100"  # no exact match -> first row
    assert _match_row([], "A", "B") is None
    assert _match_row(None, "A", "B") is None
