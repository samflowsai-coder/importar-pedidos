from __future__ import annotations

import pytest

from app.persistence import roteamento_repo as rot
from app.persistence import router


@pytest.fixture
def fresh_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield


def test_instalacao_nova_nasce_desligada(fresh_shared):
    """O teste que protege a adocao. Nao relaxar."""
    assert rot.modo() == "desligado"


def test_set_modo_grava_valor_autor_e_data(fresh_shared):
    rot.set_modo("observando", por="samuel@mm")
    assert rot.modo() == "observando"
    rot.set_modo("ligado", por="samuel@mm")
    assert rot.modo() == "ligado"
    rot.set_modo("desligado", por="samuel@mm")
    assert rot.modo() == "desligado"


def test_set_modo_recusa_valor_invalido(fresh_shared):
    with pytest.raises(ValueError):
        rot.set_modo("meio-ligado", por="samuel@mm")
    assert rot.modo() == "desligado"


def test_sombra_conta_acerto_divergencia_e_silencio(fresh_shared):
    rot.registrar_sombra(
        import_id="a", degrau="documento", env_sugerido="nasmar", env_escolhido="nasmar"
    )
    rot.registrar_sombra(
        import_id="b", degrau="historico", env_sugerido="nasmar", env_escolhido="americanense"
    )
    rot.registrar_sombra(
        import_id="c", degrau="perguntar", env_sugerido=None, env_escolhido="americanense"
    )
    t = rot.taxa(dias=30)
    assert t["total"] == 3
    assert t["bateu"] == 1
    assert t["divergiu"] == 1
    assert t["perguntar"] == 1


def test_taxa_nao_fica_negativa_se_perguntar_bater(fresh_shared):
    """Convencao: degrau='perguntar' vem com env_sugerido=None (sem sugestao,
    nao ha o que bater). Se essa convencao for violada, divergiu nao pode
    virar negativo por causa disso."""
    rot.registrar_sombra(
        import_id="a", degrau="perguntar", env_sugerido="nasmar", env_escolhido="nasmar"
    )
    t = rot.taxa(dias=30)
    assert t["total"] == 1
    assert t["bateu"] == 1
    assert t["perguntar"] == 1
    assert t["divergiu"] == 0


def test_taxa_de_periodo_vazio_nao_divide_por_zero(fresh_shared):
    t = rot.taxa(dias=30)
    assert t == {"total": 0, "bateu": 0, "divergiu": 0, "perguntar": 0, "desde": t["desde"]}


def test_pendencia_do_mesmo_arquivo_nao_duplica(fresh_shared):
    """O watcher re-varre a pasta a cada ciclo; a fila nao pode inflar."""
    for _ in range(3):
        rot.registrar_pendencia(
            sha256="abc123",
            source_path="/in/PEDIDO NBA 3.xlsx",
            env_scan_slug="nasmar",
            order_number="NBA 3",
            customer_cnpj=None,
            customer_name=None,
        )
    assert rot.contar_pendencias() == 1
    p = rot.listar_pendencias()[0]
    assert p["visto_vezes"] == 3


def test_limpar_pendencia_some_da_fila(fresh_shared):
    rot.registrar_pendencia(
        sha256="abc123",
        source_path="/in/x.xlsx",
        env_scan_slug="nasmar",
        order_number=None,
        customer_cnpj=None,
        customer_name=None,
    )
    rot.limpar_pendencia("abc123")
    assert rot.contar_pendencias() == 0
