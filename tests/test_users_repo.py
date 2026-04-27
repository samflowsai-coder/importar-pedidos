"""Tests for app.persistence.users_repo."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.persistence import db, users_repo
from app.persistence.users_repo import DuplicateEmailError, InvalidRoleError


@pytest.fixture
def sqlite_tmp(tmp_path: Path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def test_create_and_find_by_email(sqlite_tmp):
    u = users_repo.create_user(email="alice@example.com", password="strongpass1")
    assert u.id > 0
    assert u.email == "alice@example.com"
    assert u.role == "operator"
    assert u.active is True

    found = users_repo.find_by_email("alice@example.com")
    assert found is not None
    assert found.id == u.id


def test_email_lookup_is_case_insensitive(sqlite_tmp):
    users_repo.create_user(email="Bob@Example.com", password="secretpass1")
    found = users_repo.find_by_email("bob@example.com")
    assert found is not None
    found_upper = users_repo.find_by_email("BOB@EXAMPLE.COM")
    assert found_upper is not None
    assert found.id == found_upper.id


def test_duplicate_email_rejected(sqlite_tmp):
    users_repo.create_user(email="dup@x.com", password="firstpass1")
    with pytest.raises(DuplicateEmailError):
        users_repo.create_user(email="dup@x.com", password="secondpass1")
    # Even with different case
    with pytest.raises(DuplicateEmailError):
        users_repo.create_user(email="DUP@X.COM", password="thirdpass1")


def test_invalid_role_rejected(sqlite_tmp):
    with pytest.raises(InvalidRoleError):
        users_repo.create_user(email="x@x.com", password="strongpass1", role="superuser")


def test_invalid_email_rejected(sqlite_tmp):
    with pytest.raises(ValueError):
        users_repo.create_user(email="not-an-email", password="strongpass1")
    with pytest.raises(ValueError):
        users_repo.create_user(email="", password="strongpass1")


def test_password_hash_is_bcrypt(sqlite_tmp):
    """Hash stored in DB is verifiable by the passwords module."""
    from app.security.passwords import verify_password
    users_repo.create_user(email="hash@x.com", password="mypassword1")
    u = users_repo.find_by_email("hash@x.com")
    assert verify_password("mypassword1", u.password_hash)
    assert not verify_password("wrong", u.password_hash)


def test_update_last_login(sqlite_tmp):
    u = users_repo.create_user(email="ll@x.com", password="strongpass1")
    assert u.last_login_at is None
    users_repo.update_last_login(u.id)
    refreshed = users_repo.find_by_id(u.id)
    assert refreshed.last_login_at is not None


def test_update_password_hash(sqlite_tmp):
    u = users_repo.create_user(email="pw@x.com", password="originalpass")
    users_repo.update_password_hash(u.id, "$2b$12$newhashvalue")
    refreshed = users_repo.find_by_id(u.id)
    assert refreshed.password_hash == "$2b$12$newhashvalue"


def test_deactivate(sqlite_tmp):
    u = users_repo.create_user(email="d@x.com", password="strongpass1")
    users_repo.deactivate(u.id)
    refreshed = users_repo.find_by_id(u.id)
    assert refreshed.active is False


def test_list_users(sqlite_tmp):
    users_repo.create_user(email="a@x.com", password="strongpass1")
    users_repo.create_user(email="b@x.com", password="strongpass1")
    users = users_repo.list_users()
    assert len(users) == 2


def test_admin_role_accepted(sqlite_tmp):
    u = users_repo.create_user(
        email="admin@x.com", password="strongpass1", role="admin",
    )
    assert u.role == "admin"
