from __future__ import annotations

from app.sync.canonical import canonical_hash, canonical_json


def test_hash_is_deterministic():
    a = {"b": 2, "a": 1, "c": [3, 1, 2]}
    b = {"a": 1, "c": [3, 1, 2], "b": 2}
    assert canonical_hash(a) == canonical_hash(b)


def test_hash_differs_when_value_changes():
    a = {"x": 1}
    b = {"x": 2}
    assert canonical_hash(a) != canonical_hash(b)


def test_hash_changes_for_list_order():
    """Order matters in lists — components depend on positional ordering."""
    a = {"items": [1, 2, 3]}
    b = {"items": [3, 2, 1]}
    assert canonical_hash(a) != canonical_hash(b)


def test_canonical_json_no_whitespace():
    out = canonical_json({"a": 1, "b": 2})
    assert " " not in out
    assert out == '{"a":1,"b":2}'


def test_hash_is_hex_64_chars():
    h = canonical_hash({"x": 1})
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_handles_none_explicitly():
    a = {"x": None, "y": 1}
    b = {"y": 1}
    # Different shapes — different hash
    assert canonical_hash(a) != canonical_hash(b)
