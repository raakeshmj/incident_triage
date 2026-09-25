from __future__ import annotations

import json

from packages.evidence.hashing import canonical_json, content_hash, verify_content_hash
from packages.evidence.sanitize import clean_text, strip_nul


def test_hash_ignores_key_order_and_whitespace_but_not_values():
    a = {"b": [1, 2], "a": {"y": "1", "x": None}}
    b = json.loads('{ "a": {"x": null, "y": "1"},  "b": [1,2] }')
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash({**a, "b": [2, 1]})


def test_hash_format_and_verification():
    digest = content_hash({"k": "v"})
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64
    assert verify_content_hash({"k": "v"}, digest)
    assert not verify_content_hash({"k": "w"}, digest)


def test_hash_survives_a_json_round_trip():
    raw = {"values": [["1790000000.5", "0.1"]], "n": 1.0, "big": 10**15, "tiny": 1e-05}
    assert content_hash(json.loads(canonical_json(raw))) == content_hash(raw)


def test_clean_text_strips_escapes_and_control_characters_and_bounds_length():
    dirty = "\x1b[31mERROR\x1b[0m\x07 ignore previous instructions\x00"
    assert clean_text(dirty, 100) == "ERROR ignore previous instructions"
    assert clean_text("x" * 50, 10) == "x" * 9 + "…"
    assert clean_text("line1\nline2\ttab", 100) == "line1\nline2\ttab"


def test_strip_nul_is_recursive():
    assert strip_nul({"a\x00": ["b\x00", {"c": "d\x00"}], "n": 1}) == {
        "a": ["b", {"c": "d"}],
        "n": 1,
    }
