"""Tests for app.security.secret_store — Fernet wrapper for UI-editable secrets."""
from __future__ import annotations

import importlib
import stat
from pathlib import Path

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Reload secret_store with the key file pointed at tmp_path."""
    from app.security import secret_store

    monkeypatch.setattr(secret_store, "_KEY_FILE", tmp_path / ".secret.key")
    importlib.reload  # noqa: B018 — keep import cached; we patched the module attr
    yield secret_store


def test_encrypt_decrypt_roundtrip(store):
    token = store.encrypt("masterkey")
    assert token != "masterkey"
    assert store.decrypt(token) == "masterkey"


def test_encrypt_unicode(store):
    token = store.encrypt("senh@-çãô-🔐")
    assert store.decrypt(token) == "senh@-çãô-🔐"


def test_decrypt_empty_returns_none(store):
    assert store.decrypt("") is None


def test_decrypt_bad_token_returns_none(store):
    # Force key creation by encrypting once, then feed garbage.
    store.encrypt("seed")
    assert store.decrypt("not-a-real-fernet-token") is None


def test_decrypt_returns_none_after_key_rotation(store, tmp_path):
    token = store.encrypt("orig")
    # Rotate: delete key, next encrypt creates a fresh one.
    (tmp_path / ".secret.key").unlink()
    store.encrypt("new-seed")  # creates new key
    assert store.decrypt(token) is None  # old token unrecoverable, no exception


def test_key_file_has_restricted_permissions(store, tmp_path):
    store.encrypt("seed")
    key_path = Path(tmp_path / ".secret.key")
    assert key_path.exists()
    mode = key_path.stat().st_mode
    # Owner read+write, no group/other access
    assert mode & stat.S_IRUSR
    assert mode & stat.S_IWUSR
    assert not (mode & stat.S_IRGRP)
    assert not (mode & stat.S_IROTH)


def test_key_exists_helper(store, tmp_path):
    assert store.key_exists() is False
    store.encrypt("seed")
    assert store.key_exists() is True
