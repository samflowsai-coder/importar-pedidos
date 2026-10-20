from __future__ import annotations

import pytest

from app.persistence import environments_repo, router
from app.persistence import regra_faturamento_repo as repo

SAMS_CNPJS = {
    "00063960000109",
    "00063960004864",
    "00063960006131",
    "00063960006999",
    "00063960009408",
    "00063960012298",
    "00063960057984",
    "00063960058018",
    "00063960058794",
    "00063960058875",
    "00063960058956",
    "00063960059170",
}


@pytest.fixture
def shared_db(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield


@pytest.fixture
def envs_mm_nasmar(shared_db):
    for slug in ("mm", "nasmar"):
        environments_repo.create(
            slug=slug,
            name=slug.upper(),
            watch_dir=f"/x/{slug}/in",
            output_dir=f"/x/{slug}/out",
            fb_path=f"/x/{slug}.fdb",
        )


def _rede(nome):
    return next(r for r in repo.listar_redes() if r["nome"] == nome)


def test_semente_tem_as_4_redes_com_chaves(shared_db):
    redes = repo.listar_redes()
    assert [r["nome"] for r in redes] == ["Centauro", "Kallan", "Riachuelo", "Sam's Club"]
    assert all(r["env_slug"] == "mm" for r in redes)
    assert {"tipo": "raiz", "chave": "06347409"} in _rede("Centauro")["chaves"]
    sams = _rede("Sam's Club")["chaves"]
    assert len(sams) == 12
    assert {c["chave"] for c in sams} == SAMS_CNPJS
    assert all(c["tipo"] == "cnpj" for c in sams)


def test_semente_e_idempotente_e_nao_recria_rede_removida(shared_db):
    repo.remover_rede(_rede("Kallan")["id"])
    router.reset_init_cache()
    nomes = [r["nome"] for r in repo.listar_redes()]
    assert nomes == ["Centauro", "Riachuelo", "Sam's Club"]
    assert len(_rede("Sam's Club")["chaves"]) == 12


def test_politica_none_sem_config_e_com_ativa_falso(shared_db, envs_mm_nasmar):
    assert repo.config() is None
    assert repo.politica() is None
    repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=False, por="a@x")
    assert repo.config()["ativa"] is False
    assert repo.politica() is None


def test_nao_liga_com_rede_sem_chave(shared_db, envs_mm_nasmar):
    repo.salvar_rede(
        nome="Riachuelo", env_slug="mm", chaves=[], por="a@x", rede_id=_rede("Riachuelo")["id"]
    )
    with pytest.raises(ValueError, match="Riachuelo"):
        repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=True, por="a@x")
    assert repo.config() is None


def test_nao_liga_com_rede_nova_sem_chave(shared_db, envs_mm_nasmar):
    repo.salvar_rede(nome="Nova Rede", env_slug="mm", chaves=[], por="a@x")
    with pytest.raises(ValueError, match="Nova Rede"):
        repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=True, por="a@x")


def test_chave_normalizada_e_tamanho_validado(shared_db):
    rid = repo.salvar_rede(nome="X", env_slug="mm", chaves=[("raiz", "06.347.410")], por="a@x")
    assert {"tipo": "raiz", "chave": "06347410"} in _rede("X")["chaves"]
    assert rid
    with pytest.raises(ValueError):
        repo.salvar_rede(nome="Y", env_slug="mm", chaves=[("raiz", "1234567")], por="a@x")
    with pytest.raises(ValueError):
        repo.salvar_rede(nome="Z", env_slug="mm", chaves=[("cnpj", "1234567890123")], por="a@x")


def test_chave_nao_pertence_a_duas_redes(shared_db):
    with pytest.raises(ValueError, match="Centauro"):
        repo.salvar_rede(nome="Outra", env_slug="mm", chaves=[("raiz", "06.347.409")], por="a@x")
    assert "Outra" not in [r["nome"] for r in repo.listar_redes()]


def test_editar_rede_substitui_chaves_inteiras(shared_db):
    rid = _rede("Centauro")["id"]
    repo.salvar_rede(
        nome="Centauro", env_slug="mm", chaves=[("raiz", "11222333")], por="a@x", rede_id=rid
    )
    assert _rede("Centauro")["chaves"] == [{"tipo": "raiz", "chave": "11222333"}]


def test_set_config_recusa_mesma_empresa_e_empresa_inativa(shared_db, envs_mm_nasmar):
    with pytest.raises(ValueError):
        repo.set_config(env_sp="mm", env_fora_sp="mm", ativa=False, por="a@x")
    with pytest.raises(ValueError, match="fantasma"):
        repo.set_config(env_sp="mm", env_fora_sp="fantasma", ativa=False, por="a@x")
    env = environments_repo.get_by_slug("nasmar")
    environments_repo.soft_delete(env["id"])
    with pytest.raises(ValueError, match="nasmar"):
        repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=False, por="a@x")


def test_politica_monta_redes_com_raizes_e_cnpjs(shared_db, envs_mm_nasmar):
    repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=True, por="a@x")
    p = repo.politica()
    assert p.env_sp == "mm" and p.env_fora_sp == "nasmar"
    assert len(p.redes) == 4
    sams = next(r for r in p.redes if r.nome == "Sam's Club")
    assert sams.cnpjs == frozenset(SAMS_CNPJS) and sams.raizes == frozenset()
    cent = next(r for r in p.redes if r.nome == "Centauro")
    assert cent.raizes == frozenset({"06347409"}) and cent.env_slug == "mm"


def test_politica_leva_o_nome_das_empresas(shared_db, envs_mm_nasmar):
    repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=True, por="a@x")
    assert repo.politica().nomes == {"mm": "MM", "nasmar": "NASMAR"}


def test_com_regra_ligada_rede_nao_fica_sem_chave(shared_db, envs_mm_nasmar):
    repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=True, por="a@x")
    rid = _rede("Kallan")["id"]
    with pytest.raises(
        ValueError, match="Com a regra ligada, toda rede precisa de ao menos uma chave."
    ):
        repo.salvar_rede(nome="Kallan", env_slug="mm", chaves=[], por="a@x", rede_id=rid)
    assert _rede("Kallan")["chaves"] == [{"tipo": "raiz", "chave": "51540219"}]
    with pytest.raises(ValueError, match="ao menos uma chave"):
        repo.salvar_rede(nome="Nova", env_slug="mm", chaves=[], por="a@x")


# ── Revisão final ────────────────────────────────────────────────────────────


def test_semente_nao_volta_depois_que_o_admin_configurou(shared_db, envs_mm_nasmar):
    """M1: o admin apagou as 4 redes depois de configurar a regra: nunca mais semeia."""
    repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=False, por="a@x")
    for r in repo.listar_redes():
        repo.remover_rede(r["id"])
    router.reset_init_cache()
    with router.shared_connect():
        pass
    assert repo.listar_redes() == []


def test_nao_liga_com_rede_de_empresa_inativa(shared_db, envs_mm_nasmar):
    """I5: rede apontando para empresa que não é ambiente ativo barra o ligar."""
    repo.salvar_rede(
        nome="Rede Órfã", env_slug="fantasma", chaves=[("raiz", "12345678")], por="a@x"
    )
    with pytest.raises(ValueError, match="Rede Órfã"):
        repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=True, por="a@x")
    assert repo.config() is None
    # Desligada a regra continua configurável.
    repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=False, por="a@x")


def test_politica_nao_decide_por_rede_de_empresa_inativa(shared_db, envs_mm_nasmar, monkeypatch):
    """I5: empresa da rede desativada com a regra ligada: a rede sai da decisão
    (com aviso no log, só em `avaliar`) e os pedidos dela não caem na regra por UF."""
    from app.models.order import Order, OrderHeader, OrderItem
    from app.routing import regra

    avisos = []
    monkeypatch.setattr(regra.logger, "warning", lambda msg, *a, **k: avisos.append(msg))
    environments_repo.create(
        slug="terceira",
        name="Terceira",
        watch_dir="/x/t/in",
        output_dir="/x/t/out",
        fb_path="/x/t.fdb",
    )
    repo.salvar_rede(nome="Rede T", env_slug="terceira", chaves=[("raiz", "12345678")], por="a@x")
    repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=True, por="a@x")
    environments_repo.soft_delete(environments_repo.get_by_slug("terceira")["id"])

    pol = repo.politica()

    assert "terceira" not in pol.ativos
    assert avisos == []
    pedido = Order(
        header=OrderHeader(order_number="1", customer_cnpj="12345678000195", customer_name="X"),
        items=[OrderItem(description="Meia", quantity=1)],
    )

    def _nao_ler(cnpj):
        raise AssertionError("pedido da rede órfã não pode cair na UF")

    assert regra.avaliar(pedido, pol, _nao_ler) is None
    assert len(avisos) == 1 and "Rede T" in avisos[0]


def test_nao_remove_rede_com_regra_ligada(shared_db, envs_mm_nasmar):
    """I6: remover rede com a regra ligada mandaria a rede para a regra por UF."""
    repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=True, por="a@x")
    rid = _rede("Kallan")["id"]
    with pytest.raises(ValueError, match="Desligue a regra antes de remover uma rede."):
        repo.remover_rede(rid)
    assert _rede("Kallan")
    repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=False, por="a@x")
    repo.remover_rede(rid)
    assert "Kallan" not in [r["nome"] for r in repo.listar_redes()]


def test_politica_nao_avisa_rede_de_empresa_inativa(shared_db, envs_mm_nasmar, monkeypatch):
    """O aviso sai uma vez, em `avaliar`; carregar a política não repete por pedido."""
    from app.utils.logger import logger

    repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=True, por="a@x")
    so_nasmar = [e for e in environments_repo.list_active() if e["slug"] == "nasmar"]
    monkeypatch.setattr(environments_repo, "list_active", lambda: so_nasmar)
    avisos: list[str] = []
    sink = logger.add(avisos.append, level="WARNING")
    try:
        assert repo.politica() is not None
    finally:
        logger.remove(sink)
    assert avisos == []
