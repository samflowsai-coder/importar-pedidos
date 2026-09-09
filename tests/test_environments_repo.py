"""CRUD da tabela `environments` em app_shared.db."""

from __future__ import annotations

import pytest

from app.erp.fiscal import perfil_para
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
    # Espaços não são aceitos
    with pytest.raises(ValueError):
        environments_repo.create(
            slug="mm prod",
            name="MM",
            watch_dir="/x",
            output_dir="/y",
            fb_path="/z.fdb",
        )
    # Caracteres especiais
    with pytest.raises(ValueError):
        environments_repo.create(
            slug="mm@prod",
            name="MM",
            watch_dir="/x",
            output_dir="/y",
            fb_path="/z.fdb",
        )


def test_create_normalizes_uppercase_slug_to_lowercase(fresh_shared):
    """Slug com maiúscula é normalizado, não rejeitado — UX permissiva."""
    env = environments_repo.create(
        slug="MM",
        name="MM",
        watch_dir="/x",
        output_dir="/y",
        fb_path="/z.fdb",
    )
    assert env["slug"] == "mm"
    env2 = environments_repo.create(
        slug="  Nasmar  ",
        name="Nasmar",
        watch_dir="/x",
        output_dir="/y",
        fb_path="/z.fdb",
    )
    assert env2["slug"] == "nasmar"


def test_create_requires_name(fresh_shared):
    with pytest.raises(ValueError):
        environments_repo.create(
            slug="mm",
            name="   ",
            watch_dir="/a",
            output_dir="/b",
            fb_path="/c.fdb",
        )


def test_create_rejects_duplicate_slug(fresh_shared):
    environments_repo.create(
        slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
    )
    with pytest.raises(environments_repo.SlugTaken):
        environments_repo.create(
            slug="mm", name="MM2", watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
        )


def test_update_does_not_change_slug(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
    )
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
        slug="mm",
        name="MM",
        watch_dir="/a",
        output_dir="/b",
        fb_path="/c.fdb",
        fb_password="masterkey",
    )
    pw = environments_repo.get_password(env["id"])
    assert pw == "masterkey"


def test_password_none_when_absent(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
    )
    assert environments_repo.get_password(env["id"]) is None


def test_update_password_keeps_existing_when_none(fresh_shared):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir="/a",
        output_dir="/b",
        fb_path="/c.fdb",
        fb_password="orig",
    )
    environments_repo.update(env["id"], name="MM2", fb_password=None)
    assert environments_repo.get_password(env["id"]) == "orig"


def test_update_password_clears_with_empty_string(fresh_shared):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir="/a",
        output_dir="/b",
        fb_path="/c.fdb",
        fb_password="orig",
    )
    environments_repo.update(env["id"], fb_password="")
    assert environments_repo.get_password(env["id"]) is None


def test_update_password_replaces(fresh_shared):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir="/a",
        output_dir="/b",
        fb_path="/c.fdb",
        fb_password="old",
    )
    environments_repo.update(env["id"], fb_password="new")
    assert environments_repo.get_password(env["id"]) == "new"


def test_soft_delete(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
    )
    environments_repo.soft_delete(env["id"])
    after = environments_repo.get(env["id"])
    assert after["is_active"] == 0
    actives = environments_repo.list_active()
    assert all(e["id"] != env["id"] for e in actives)


def test_list_active_orders_by_name(fresh_shared):
    environments_repo.create(
        slug="nasmar", name="Nasmar", watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
    )
    environments_repo.create(
        slug="mm", name="MM Calçados", watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
    )
    rows = environments_repo.list_active()
    assert [e["slug"] for e in rows] == ["mm", "nasmar"]  # MM (M) < Nasmar (N) por nome


def test_list_all_includes_inactive(fresh_shared):
    a = environments_repo.create(
        slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
    )
    environments_repo.create(
        slug="nasmar", name="Nasmar", watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
    )
    environments_repo.soft_delete(a["id"])
    rows = environments_repo.list_all()
    slugs = {e["slug"] for e in rows}
    assert slugs == {"mm", "nasmar"}


def test_get_by_slug(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
    )
    found = environments_repo.get_by_slug("mm")
    assert found["id"] == env["id"]
    assert environments_repo.get_by_slug("inexistente") is None


def test_to_fb_config_extracts_password(fresh_shared):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir="/a",
        output_dir="/b",
        fb_path="/c.fdb",
        fb_host="192.168.1.10",
        fb_port="3050",
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


# ── FlowPCP per-ambiente (token cifrado via secret_store) ─────────────────────


def test_flowpcp_defaults_disabled(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
    )
    got = environments_repo.get(env["id"])
    assert got["flowpcp_enabled"] == 0
    assert got["flowpcp_timezone"] == "America/Sao_Paulo"
    assert got["flowpcp_poll_interval_s"] == 30
    assert environments_repo.get_flowpcp_token(env["id"]) is None


def test_flowpcp_config_round_trip(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
    )
    environments_repo.set_flowpcp_config(
        env["id"],
        enabled=True,
        base_url="https://flow.test",
        tenant_id="uuid-mm",
        dry_run=True,
        poll_interval_s=45,
        service_token="svc-tok",
    )
    got = environments_repo.get(env["id"])
    assert got["flowpcp_enabled"] == 1
    assert got["flowpcp_base_url"] == "https://flow.test"
    assert got["flowpcp_tenant_id"] == "uuid-mm"
    assert got["flowpcp_dry_run"] == 1
    assert got["flowpcp_poll_interval_s"] == 45
    # token nunca volta no public view
    assert "flowpcp_service_token_enc" not in got
    assert environments_repo.get_flowpcp_token(env["id"]) == "svc-tok"


def test_flowpcp_token_keep_replace_clear(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
    )
    base = dict(enabled=True, base_url="x", tenant_id="t")
    environments_repo.set_flowpcp_config(env["id"], **base, service_token="orig")
    # None → mantém
    environments_repo.set_flowpcp_config(env["id"], **base, service_token=None)
    assert environments_repo.get_flowpcp_token(env["id"]) == "orig"
    # "..." → substitui
    environments_repo.set_flowpcp_config(env["id"], **base, service_token="new")
    assert environments_repo.get_flowpcp_token(env["id"]) == "new"
    # "" → limpa
    environments_repo.set_flowpcp_config(env["id"], **base, service_token="")
    assert environments_repo.get_flowpcp_token(env["id"]) is None


def test_flowpcp_disable_keeps_token(fresh_shared):
    """Desligar não apaga o token — re-ligar não exige redigitar."""
    env = environments_repo.create(
        slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
    )
    environments_repo.set_flowpcp_config(
        env["id"], enabled=True, base_url="x", tenant_id="t", service_token="keepme"
    )
    environments_repo.set_flowpcp_config(
        env["id"], enabled=False, base_url="x", tenant_id="t", service_token=None
    )
    assert environments_repo.get(env["id"])["flowpcp_enabled"] == 0
    assert environments_repo.get_flowpcp_token(env["id"]) == "keepme"


# ── fb_path sanitization ─────────────────────────────────────────────────────
# Finder "Copy as Pathname" e cmd do Windows costumam embrulhar paths em
# aspas. Salvar bruto quebra a conexão Firebird com "io error: file not found".


def test_create_strips_wrapping_quotes_from_fb_path(fresh_shared):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir="/a",
        output_dir="/b",
        fb_path="'/Users/me/db.fdb'",
    )
    assert env["fb_path"] == "/Users/me/db.fdb"


def test_create_strips_wrapping_double_quotes_and_whitespace(fresh_shared):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir="/a",
        output_dir="/b",
        fb_path='  "/Users/me/db.fdb"  ',
    )
    assert env["fb_path"] == "/Users/me/db.fdb"


def test_update_strips_wrapping_quotes_from_fb_path(fresh_shared):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir="/a",
        output_dir="/b",
        fb_path="/clean.fdb",
    )
    updated = environments_repo.update(env["id"], fb_path="'/new/path.fdb'")
    assert updated["fb_path"] == "/new/path.fdb"


def test_to_fb_config_strips_legacy_quoted_path(fresh_shared):
    """Dados legados na DB (gravados antes do fix) ainda podem ter aspas —
    `to_fb_config` normaliza na leitura."""
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir="/a",
        output_dir="/b",
        fb_path="/clean.fdb",
    )
    # Simula linha legada com aspas literais na DB (bypassa o sanitizer do update).
    with router.shared_connect() as conn:
        conn.execute(
            "UPDATE environments SET fb_path = ? WHERE id = ?",
            ("'/legacy/path.fdb'", env["id"]),
        )
    env_legacy = environments_repo.get(env["id"])
    cfg = environments_repo.to_fb_config(env_legacy)
    assert cfg["path"] == "/legacy/path.fdb"


def test_fiscal_codfigfiscal_round_trip(fresh_shared):
    """Perfil fiscal via environments_repo + perfil_para: caminho ponta a ponta."""
    # Cria ambiente (default MM)
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir="/a",
        output_dir="/b",
        fb_path="/c.fdb",
    )
    # Verify default (MM=1)
    env_mm = environments_repo.get(env["id"])
    assert env_mm["fiscal_codfigfiscal"] is None
    perfil_mm = perfil_para(env_mm)
    assert perfil_mm.codfigfiscal == 1

    # Update para Nasmar=5
    env_nasmar = environments_repo.update(env["id"], fiscal_codfigfiscal=5)
    assert env_nasmar["fiscal_codfigfiscal"] == 5
    perfil_nasmar = perfil_para(env_nasmar)
    assert perfil_nasmar.codfigfiscal == 5


# ── CNPJ do ambiente (chave do roteamento pelo documento) ─────────────────────


def test_cnpj_e_gravado_em_digitos(fresh_shared):
    """Admin digita formatado; o banco guarda so os digitos."""
    env = environments_repo.create(
        slug="nasmar", name="Nasmar",
        watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        cnpj="34.513.679/0001-34",
    )
    assert env["cnpj"] == "34513679000134"


def test_find_by_cnpj_casa_formatado_ou_nao(fresh_shared):
    environments_repo.create(
        slug="mm", name="MM Americanense",
        watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        cnpj="35394871000111",
    )
    achado = environments_repo.find_by_cnpj("35.394.871/0001-11")
    assert achado is not None
    assert achado["slug"] == "mm"
    assert environments_repo.find_by_cnpj("00000000000000") is None
    assert environments_repo.find_by_cnpj("") is None
    assert environments_repo.find_by_cnpj(None) is None


def test_find_by_cnpj_ignora_ambiente_inativo(fresh_shared):
    """Ambiente desativado nao roteia nada — some do lookup."""
    env = environments_repo.create(
        slug="velho", name="Empresa antiga",
        watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        cnpj="11222333000144",
    )
    environments_repo.soft_delete(env["id"])
    assert environments_repo.find_by_cnpj("11222333000144") is None


def test_update_limpa_cnpj_com_string_vazia(fresh_shared):
    """None mantem, "" limpa — mesma semantica de fb_password."""
    env = environments_repo.create(
        slug="mm2", name="MM",
        watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        cnpj="35394871000111",
    )
    assert environments_repo.update(env["id"], name="MM 2")["cnpj"] == "35394871000111"
    assert environments_repo.update(env["id"], cnpj="")["cnpj"] is None


# ── Unicidade de CNPJ entre ambientes (índice único parcial) ──────────────────
# Sem UNIQUE, dois ambientes ATIVOS com o mesmo CNPJ fariam find_by_cnpj devolver
# o que o LIMIT 1 pegar — o pedido vai pra empresa errada sem ninguém perceber.


def test_create_rejeita_cnpj_duplicado(fresh_shared):
    environments_repo.create(
        slug="mm", name="MM",
        watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        cnpj="35394871000111",
    )
    with pytest.raises(environments_repo.CnpjTaken):
        environments_repo.create(
            slug="mm-duplicado", name="MM Duplicado",
            watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
            cnpj="35394871000111",
        )


def test_update_rejeita_cnpj_ja_usado_por_outro_ambiente(fresh_shared):
    environments_repo.create(
        slug="nasmar", name="Nasmar",
        watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        cnpj="34513679000134",
    )
    mm = environments_repo.create(
        slug="mm", name="MM",
        watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        cnpj="35394871000111",
    )
    with pytest.raises(environments_repo.CnpjTaken):
        environments_repo.update(mm["id"], cnpj="34513679000134")


def test_multiplos_ambientes_com_cnpj_null_convivem(fresh_shared):
    """NULL nao e valor — e o estado de todo ambiente hoje. O indice parcial
    (WHERE cnpj IS NOT NULL) tem que deixar N ambientes sem CNPJ conviverem."""
    environments_repo.create(slug="a", name="A", watch_dir="/x", output_dir="/y", fb_path="/z.fdb")
    environments_repo.create(slug="b", name="B", watch_dir="/x", output_dir="/y", fb_path="/z.fdb")
    environments_repo.create(slug="c", name="C", watch_dir="/x", output_dir="/y", fb_path="/z.fdb")
    slugs = {e["slug"] for e in environments_repo.list_all()}
    assert slugs == {"a", "b", "c"}


def test_create_rejeita_cnpj_duplicado_com_formatacao_diferente(fresh_shared):
    """O indice e sobre a coluna normalizada — formatado ou nao colide igual."""
    environments_repo.create(
        slug="nasmar", name="Nasmar",
        watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        cnpj="34.513.679/0001-34",
    )
    with pytest.raises(environments_repo.CnpjTaken):
        environments_repo.create(
            slug="nasmar-2", name="Nasmar 2",
            watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
            cnpj="34513679000134",
        )
