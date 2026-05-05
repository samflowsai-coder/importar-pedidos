"""CRUD da tabela `environments` em app_shared.db."""
from __future__ import annotations

import pytest

from app.persistence import environments_repo, router


@pytest.fixture
def fresh_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield


def test_create_and_get(fresh_shared):
    env = environments_repo.create(
        slug="mm",
        name="MM Calçados",
        watch_dir="/tmp/mm/in",
        output_dir="/tmp/mm/out",
        fb_path="/tmp/mm.fdb",
        fb_password="secret123",
    )
    assert env["slug"] == "mm"
    assert env["name"] == "MM Calçados"
    assert env["is_active"] == 1
    # senha nunca volta no public view
    assert "fb_password_enc" not in env
    assert "fb_password" not in env

    same = environments_repo.get(env["id"])
    assert same["id"] == env["id"]
    assert same["fb_path"] == "/tmp/mm.fdb"


def test_create_rejects_invalid_slug(fresh_shared):
    with pytest.raises(ValueError):
        environments_repo.create(
            slug="MM",  # uppercase
            name="MM",
            watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        )
    with pytest.raises(ValueError):
        environments_repo.create(
            slug="mm prod",
            name="MM",
            watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        )


def test_create_requires_name(fresh_shared):
    with pytest.raises(ValueError):
        environments_repo.create(
            slug="mm", name="   ",
            watch_dir="/a", output_dir="/b", fb_path="/c.fdb",
        )


def test_create_rejects_duplicate_slug(fresh_shared):
    environments_repo.create(slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    with pytest.raises(environments_repo.SlugTaken):
        environments_repo.create(slug="mm", name="MM2", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")


def test_update_does_not_change_slug(fresh_shared):
    env = environments_repo.create(slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    updated = environments_repo.update(
        env["id"],
        name="MM Renomeado",
        watch_dir="/novo",
    )
    assert updated["name"] == "MM Renomeado"
    assert updated["watch_dir"] == "/novo"
    assert updated["slug"] == "mm"


def test_password_round_trip(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM",
        watch_dir="/a", output_dir="/b", fb_path="/c.fdb",
        fb_password="masterkey",
    )
    pw = environments_repo.get_password(env["id"])
    assert pw == "masterkey"


def test_password_none_when_absent(fresh_shared):
    env = environments_repo.create(slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    assert environments_repo.get_password(env["id"]) is None


def test_update_password_keeps_existing_when_none(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM",
        watch_dir="/a", output_dir="/b", fb_path="/c.fdb",
        fb_password="orig",
    )
    environments_repo.update(env["id"], name="MM2", fb_password=None)
    assert environments_repo.get_password(env["id"]) == "orig"


def test_update_password_clears_with_empty_string(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM",
        watch_dir="/a", output_dir="/b", fb_path="/c.fdb",
        fb_password="orig",
    )
    environments_repo.update(env["id"], fb_password="")
    assert environments_repo.get_password(env["id"]) is None


def test_update_password_replaces(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM",
        watch_dir="/a", output_dir="/b", fb_path="/c.fdb",
        fb_password="old",
    )
    environments_repo.update(env["id"], fb_password="new")
    assert environments_repo.get_password(env["id"]) == "new"


def test_soft_delete(fresh_shared):
    env = environments_repo.create(slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    environments_repo.soft_delete(env["id"])
    after = environments_repo.get(env["id"])
    assert after["is_active"] == 0
    actives = environments_repo.list_active()
    assert all(e["id"] != env["id"] for e in actives)


def test_list_active_orders_by_name(fresh_shared):
    environments_repo.create(slug="nasmar", name="Nasmar", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    environments_repo.create(slug="mm",     name="MM Calçados", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    rows = environments_repo.list_active()
    assert [e["slug"] for e in rows] == ["mm", "nasmar"]  # MM (M) < Nasmar (N) por nome


def test_list_all_includes_inactive(fresh_shared):
    a = environments_repo.create(slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    environments_repo.create(slug="nasmar", name="Nasmar", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    environments_repo.soft_delete(a["id"])
    rows = environments_repo.list_all()
    slugs = {e["slug"] for e in rows}
    assert slugs == {"mm", "nasmar"}


def test_get_by_slug(fresh_shared):
    env = environments_repo.create(slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    found = environments_repo.get_by_slug("mm")
    assert found["id"] == env["id"]
    assert environments_repo.get_by_slug("inexistente") is None


def test_to_fb_config_extracts_password(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM",
        watch_dir="/a", output_dir="/b", fb_path="/c.fdb",
        fb_host="192.168.1.10", fb_port="3050",
        fb_password="masterkey",
    )
    cfg = environments_repo.to_fb_config(env)
    assert cfg["path"] == "/c.fdb"
    assert cfg["host"] == "192.168.1.10"
    assert cfg["port"] == "3050"
    assert cfg["password"] == "masterkey"
    assert cfg["user"] == "SYSDBA"
    assert cfg["charset"] == "WIN1252"


def test_get_returns_none_for_missing(fresh_shared):
    assert environments_repo.get("nope") is None
