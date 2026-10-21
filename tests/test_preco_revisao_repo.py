"""Revisão do pedido e decisões do Flow (app/persistence/preco_revisao_repo.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.persistence import preco_revisao_repo as rev
from app.persistence import router

AGORA = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
DEPOIS = AGORA + timedelta(minutes=5)
IMP = "imp-1"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as c:
        yield c


def _corpo(tag="a"):
    return {
        "schema": "precos.aprovacao.v1",
        "tipo": "pedido",
        "externalId": IMP,
        "empresaFaturadoraCnpj": "11111111000191",
        "tag": tag,
        "itens": [{"itemChave": f"{IMP}:0", "erpProdutoId": "123"}],
    }


def _decisao(decisao_id, revisao, item=0, *, resultado="acordo_comercial", preco=5400, em=None):
    return {
        "decisaoId": decisao_id,
        "externalId": IMP,
        "empresaFaturadoraCnpj": "11111111000191",
        "revisao": revisao,
        "itemChave": f"{IMP}:{item}",
        "erpProdutoId": "123",
        "resultado": resultado,
        "precoAprovadoCentavos": None if resultado == "recusado" else preco,
        "precoTabelaNoMomentoCentavos": 5000,
        "precoIdCriado": None,
        "motivoRecusa": "fora da política" if resultado == "recusado" else None,
        "observacao": None,
        "decisor": "aprovador",
        "decididoEm": em or "2026-10-18T12:01:00.000Z",
    }


def _situacoes(conn):
    return [
        (r["revisao"], r["situacao"])
        for r in conn.execute(
            "SELECT revisao, situacao FROM preco_revisao WHERE import_id = ? ORDER BY revisao",
            (IMP,),
        )
    ]


def test_primeira_revisao_e_1_pendente_envio(conn):
    assert rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA) == 1
    v = rev.vigente(conn, IMP)
    assert v["revisao"] == 1
    assert v["situacao"] == "pendente_envio"
    assert v["conteudo_hash"] == "sha256:a"
    assert v["corpo"] == {**_corpo(), "revisao": 1}


def test_mesmo_hash_devolve_a_vigente(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    assert rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=DEPOIS) == 1
    assert _situacoes(conn) == [(1, "pendente_envio")]


def test_a_b_a_gera_tres_revisoes(conn):
    assert rev.garantir_revisao(conn, IMP, _corpo("a"), "sha256:a", agora=AGORA) == 1
    assert rev.garantir_revisao(conn, IMP, _corpo("b"), "sha256:b", agora=AGORA) == 2
    assert rev.garantir_revisao(conn, IMP, _corpo("a"), "sha256:a", agora=AGORA) == 3
    assert _situacoes(conn) == [(1, "substituida"), (2, "substituida"), (3, "pendente_envio")]


def test_recusada_flow_com_o_mesmo_corpo_gera_revisao_nova(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    rev.marcar_recusa_flow(conn, IMP, 1, "rede_desconhecida", em=AGORA)
    assert rev.vigente(conn, IMP)["situacao"] == "recusada_flow"
    assert rev.vigente(conn, IMP)["erro_definitivo"] == "rede_desconhecida"

    assert rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=DEPOIS) == 2
    assert _situacoes(conn) == [(1, "substituida"), (2, "pendente_envio")]
    assert rev.vigente(conn, IMP)["corpo"]["revisao"] == 2


def test_subir_piso_renumera_a_vigente(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    assert rev.subir_piso(conn, IMP, 7, agora=AGORA) == 8
    v = rev.vigente(conn, IMP)
    assert (v["revisao"], v["situacao"], v["corpo"]["revisao"]) == (8, "pendente_envio", 8)
    assert _situacoes(conn) == [(1, "substituida"), (8, "pendente_envio")]
    # a próxima mudança parte do piso
    assert rev.garantir_revisao(conn, IMP, _corpo("b"), "sha256:b", agora=AGORA) == 9


def test_marcar_enviada_so_em_pendente_envio(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    rev.marcar_enviada(conn, IMP, 1, {"situacao": "pendente"}, em=AGORA)
    v = rev.vigente(conn, IMP)
    assert v["situacao"] == "enviada" and v["enviada_em"] == AGORA.isoformat()
    rev.marcar_recusa_flow(conn, IMP, 1, "produto_desconhecido", em=AGORA)
    assert rev.vigente(conn, IMP)["situacao"] == "enviada"


def test_corrida_201_da_revisao_em_voo_nao_ressuscita(conn):
    rev.garantir_revisao(conn, IMP, _corpo("a"), "sha256:a", agora=AGORA)
    em_voo = rev.pendentes_envio(conn, 10)
    assert [p["revisao"] for p in em_voo] == [1]
    rev.garantir_revisao(conn, IMP, _corpo("b"), "sha256:b", agora=AGORA)  # portão: rev 2

    rev.marcar_enviada(conn, IMP, 1, {"situacao": "pendente"}, em=DEPOIS)  # 201 da rev 1

    assert _situacoes(conn) == [(1, "substituida"), (2, "pendente_envio")]
    assert rev.vigente(conn, IMP)["revisao"] == 2
    assert [p["revisao"] for p in rev.pendentes_envio(conn, 10)] == [2]


def test_pendentes_envio_so_a_vigente_e_sem_retirado(conn):
    rev.garantir_revisao(conn, IMP, _corpo("a"), "sha256:a", agora=AGORA)
    rev.garantir_revisao(conn, "imp-2", _corpo("x"), "sha256:x", agora=AGORA)
    rev.garantir_revisao(conn, IMP, _corpo("b"), "sha256:b", agora=AGORA)
    assert sorted((p["import_id"], p["revisao"]) for p in rev.pendentes_envio(conn, 10)) == [
        (IMP, 2),
        ("imp-2", 1),
    ]
    rev.retirar(conn, "imp-2", "cancelado", em=AGORA)
    assert [(p["import_id"], p["revisao"]) for p in rev.pendentes_envio(conn, 10)] == [(IMP, 2)]
    assert rev.pendentes_envio(conn, 10)[0]["corpo"]["revisao"] == 2


def test_gravar_decisao_da_vigente_aplicada_e_de_outra_descartada(conn):
    rev.garantir_revisao(conn, IMP, _corpo("a"), "sha256:a", agora=AGORA)
    rev.garantir_revisao(conn, IMP, _corpo("b"), "sha256:b", agora=AGORA)

    # decisão da revisão 1 chega depois da 2
    assert rev.gravar_decisao(conn, _decisao("d1", 1), agora=AGORA) == "descartada"
    assert rev.gravar_decisao(conn, _decisao("d2", 2), agora=AGORA) == "aplicada"

    assert set(rev.decisoes_da_vigente(conn, IMP)) == {f"{IMP}:0"}
    assert rev.decisoes_da_vigente(conn, IMP)[f"{IMP}:0"]["decisao_id"] == "d2"


def test_gravar_decisao_idempotente_por_id(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    assert rev.gravar_decisao(conn, _decisao("d1", 1), agora=AGORA) == "aplicada"
    rev.garantir_revisao(conn, IMP, _corpo("b"), "sha256:b", agora=AGORA)
    # repetida depois da troca de revisão: devolve o que foi gravado
    assert rev.gravar_decisao(conn, _decisao("d1", 1), agora=DEPOIS) == "aplicada"
    assert conn.execute("SELECT COUNT(*) FROM preco_decisao").fetchone()[0] == 1


def test_decisao_de_pedido_desconhecido_descartada(conn):
    assert rev.gravar_decisao(conn, _decisao("d9", 1), agora=AGORA) == "descartada"


def test_duas_decisoes_mesma_chave_vence_a_mais_recente(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    rev.gravar_decisao(
        conn, _decisao("d2", 1, preco=5500, em="2026-10-18T12:05:00.000Z"), agora=AGORA
    )
    rev.gravar_decisao(
        conn, _decisao("d1", 1, preco=5400, em="2026-10-18T12:01:00.000Z"), agora=AGORA
    )
    assert rev.decisoes_da_vigente(conn, IMP)[f"{IMP}:0"]["preco_aprovado_centavos"] == 5500


def test_confirmacoes_pendentes_e_marcar_confirmada(conn):
    rev.garantir_revisao(conn, IMP, _corpo("a"), "sha256:a", agora=AGORA)
    rev.garantir_revisao(conn, IMP, _corpo("b"), "sha256:b", agora=AGORA)
    rev.gravar_decisao(conn, _decisao("d1", 1), agora=AGORA)
    rev.gravar_decisao(conn, _decisao("d2", 2), agora=AGORA)

    pend = rev.confirmacoes_pendentes(conn, 10)
    assert [(c["decisao_id"], c["confirmacao"]) for c in pend] == [
        ("d1", "descartada"),
        ("d2", "aplicada"),
    ]
    rev.marcar_confirmada(conn, ["d1"], em=DEPOIS)
    assert [c["decisao_id"] for c in rev.confirmacoes_pendentes(conn, 10)] == ["d2"]


# ── Retirada ─────────────────────────────────────────────────────────────────


def test_retirar_nunca_enviada_nada_vai_ao_flow(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    rev.retirar(conn, IMP, "pedido cancelado", em=AGORA)
    assert rev.vigente(conn, IMP) is None
    assert rev.pendentes_envio(conn, 10) == []
    assert rev.retiradas_pendentes(conn, 10) == []


def test_retirar_enviada_enfileira_retirada_dela(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    rev.marcar_enviada(conn, IMP, 1, {}, em=AGORA)
    rev.retirar(conn, IMP, "pedido cancelado", em=DEPOIS)

    assert rev.vigente(conn, IMP) is None
    pend = rev.retiradas_pendentes(conn, 10)
    assert [(p["import_id"], p["revisao"], p["retirada_motivo"]) for p in pend] == [
        (IMP, 1, "pedido cancelado")
    ]
    rev.marcar_retirada_enviada(conn, IMP, 1, em=DEPOIS)
    assert rev.retiradas_pendentes(conn, 10) == []


def test_retirar_descarta_pendente_e_retira_a_ultima_enviada(conn):
    rev.garantir_revisao(conn, IMP, _corpo("a"), "sha256:a", agora=AGORA)
    rev.marcar_enviada(conn, IMP, 1, {}, em=AGORA)
    rev.garantir_revisao(conn, IMP, _corpo("b"), "sha256:b", agora=AGORA)  # rev 2 pendente

    rev.retirar(conn, IMP, "pedido cancelado", em=DEPOIS)

    assert [p["revisao"] for p in rev.retiradas_pendentes(conn, 10)] == [1]
    assert rev.pendentes_envio(conn, 10) == []
    assert _situacoes(conn) == [(1, "retirada"), (2, "retirada")]


def test_retirar_e_idempotente(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    rev.marcar_enviada(conn, IMP, 1, {}, em=AGORA)
    rev.retirar(conn, IMP, "x", em=AGORA)
    rev.marcar_retirada_enviada(conn, IMP, 1, em=AGORA)
    rev.retirar(conn, IMP, "x", em=DEPOIS)
    assert rev.retiradas_pendentes(conn, 10) == []


def test_reaberto_depois_da_retirada_nao_manda_retirada_velha(conn):
    rev.garantir_revisao(conn, IMP, _corpo("a"), "sha256:a", agora=AGORA)
    rev.marcar_enviada(conn, IMP, 1, {}, em=AGORA)
    rev.retirar(conn, IMP, "x", em=AGORA)
    assert rev.garantir_revisao(conn, IMP, _corpo("b"), "sha256:b", agora=DEPOIS) == 2
    # a revisão 2 substitui a 1 no Flow: a retirada da 1 retiraria a 2 (409 → vigente)
    assert rev.retiradas_pendentes(conn, 10) == []
    assert [p["revisao"] for p in rev.pendentes_envio(conn, 10)] == [2]


# ── Fix round 1 ──────────────────────────────────────────────────────────────


def test_rev1_em_voo_rev2_cancel_201_da_rev1_deixa_retirada_da_rev1(conn):
    rev.garantir_revisao(conn, IMP, _corpo("a"), "sha256:a", agora=AGORA)
    rev.garantir_revisao(conn, IMP, _corpo("b"), "sha256:b", agora=AGORA)
    rev.retirar(conn, IMP, "pedido cancelado", em=AGORA)
    assert rev.retiradas_pendentes(conn, 10) == []  # o Flow ainda não conhece nenhuma

    rev.marcar_enviada(conn, IMP, 1, {"situacao": "pendente"}, em=DEPOIS)  # 201 atrasado

    pend = rev.retiradas_pendentes(conn, 10)
    assert [(p["revisao"], p["retirada_motivo"]) for p in pend] == [(1, "pedido cancelado")]
    assert rev.vigente(conn, IMP) is None


def test_rev1_tentada_sem_resposta_cancel_enfileira_retirada(conn):
    rev.garantir_revisao(conn, IMP, _corpo("a"), "sha256:a", agora=AGORA)
    assert rev.marcar_tentada(conn, IMP, 1, em=AGORA) is True  # POST sai, resposta perdida
    rev.retirar(conn, IMP, "pedido cancelado", em=DEPOIS)
    assert [p["revisao"] for p in rev.retiradas_pendentes(conn, 10)] == [1]


def test_tentada_em_voo_substituida_e_a_ultima_conhecida_no_cancel(conn):
    rev.garantir_revisao(conn, IMP, _corpo("a"), "sha256:a", agora=AGORA)
    rev.marcar_tentada(conn, IMP, 1, em=AGORA)
    rev.garantir_revisao(conn, IMP, _corpo("b"), "sha256:b", agora=AGORA)  # rev 2 nunca tentada
    rev.retirar(conn, IMP, "x", em=DEPOIS)
    assert [p["revisao"] for p in rev.retiradas_pendentes(conn, 10)] == [1]


def test_marcar_tentada_so_em_pendente_envio(conn):
    rev.garantir_revisao(conn, IMP, _corpo("a"), "sha256:a", agora=AGORA)
    rev.retirar(conn, IMP, "x", em=AGORA)
    assert rev.marcar_tentada(conn, IMP, 1, em=AGORA) is False  # não envia o retirado


def test_decisao_de_outro_produto_ou_empresa_descartada(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    d = _decisao("d1", 1)
    d["erpProdutoId"] = "999"
    assert rev.gravar_decisao(conn, d, agora=AGORA) == "descartada"
    d = _decisao("d2", 1)
    d["empresaFaturadoraCnpj"] = "22222222000182"
    assert rev.gravar_decisao(conn, d, agora=AGORA) == "descartada"
    d = _decisao("d3", 1, item=7)  # itemChave fora do corpo
    assert rev.gravar_decisao(conn, d, agora=AGORA) == "descartada"
    assert rev.decisoes_da_vigente(conn, IMP) == {}


def test_empate_de_decidido_em_vence_o_maior_decisao_id(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    rev.gravar_decisao(conn, _decisao("d-a", 1, preco=5400), agora=AGORA)
    rev.gravar_decisao(conn, _decisao("d-b", 1, preco=5500), agora=AGORA)
    rev.gravar_decisao(conn, _decisao("d-0", 1, preco=5300), agora=AGORA)
    assert rev.decisoes_da_vigente(conn, IMP)[f"{IMP}:0"]["decisao_id"] == "d-b"


# ── Fix round 2 ──────────────────────────────────────────────────────────────


def test_422_limpa_tentada_e_cancel_retira_a_que_o_flow_guarda(conn):
    rev.garantir_revisao(conn, IMP, _corpo("a"), "sha256:a", agora=AGORA)
    rev.marcar_tentada(conn, IMP, 1, em=AGORA)
    rev.marcar_enviada(conn, IMP, 1, {}, em=AGORA)
    rev.garantir_revisao(conn, IMP, _corpo("b"), "sha256:b", agora=AGORA)
    rev.marcar_tentada(conn, IMP, 2, em=AGORA)
    rev.marcar_recusa_flow(conn, IMP, 2, "produto_desconhecido", em=AGORA)

    rev.retirar(conn, IMP, "cancelado", em=DEPOIS)

    assert [p["revisao"] for p in rev.retiradas_pendentes(conn, 10)] == [1]


# ─── bloqueio local (corpo fora do contrato, 413, 400 payload_invalido) ──────


def test_bloqueio_local_sai_da_fila_e_limpa_a_tentativa(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    assert rev.marcar_tentada(conn, IMP, 1, em=AGORA)
    rev.marcar_bloqueio_local(conn, IMP, 1, "pedido grande demais para o Flow", em=DEPOIS)
    v = rev.vigente(conn, IMP)
    assert v["situacao"] == "pendente_envio"
    assert v["bloqueio_local"] == "pedido grande demais para o Flow"
    assert v["tentada_em"] is None
    assert rev.pendentes_envio(conn, 10) == []


def test_bloqueio_local_com_o_mesmo_conteudo_nao_recria_revisao(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    rev.marcar_bloqueio_local(conn, IMP, 1, "x", em=AGORA)
    assert rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=DEPOIS) == 1
    assert _situacoes(conn) == [(1, "pendente_envio")]
    assert rev.garantir_revisao(conn, IMP, _corpo("b"), "sha256:b", agora=DEPOIS) == 2
    assert _situacoes(conn) == [(1, "substituida"), (2, "pendente_envio")]
    assert rev.vigente(conn, IMP)["bloqueio_local"] is None


def test_bloqueio_local_so_vale_para_pendente_envio(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    rev.marcar_enviada(conn, IMP, 1, {}, em=AGORA)
    rev.marcar_bloqueio_local(conn, IMP, 1, "x", em=AGORA)
    assert rev.vigente(conn, IMP)["bloqueio_local"] is None


def test_contagem_por_situacao(conn):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    rev.garantir_revisao(conn, "imp-2", _corpo(), "sha256:a", agora=AGORA)
    rev.garantir_revisao(conn, "imp-3", _corpo(), "sha256:a", agora=AGORA)
    rev.marcar_bloqueio_local(conn, "imp-2", 1, "x", em=AGORA)
    rev.marcar_recusa_flow(conn, "imp-3", 1, "rede_desconhecida", em=AGORA)
    assert rev.contagem_por_situacao(conn) == {
        "pendente_envio": 1,
        "bloqueada_local": 1,
        "recusada_flow": 1,
    }


def test_pendentes_envio_quem_ja_tentou_vai_para_o_fim(conn):
    rev.garantir_revisao(conn, "imp-a", _corpo(), "sha256:a", agora=AGORA)
    rev.garantir_revisao(conn, "imp-b", _corpo(), "sha256:b", agora=AGORA + timedelta(seconds=1))
    assert [p["import_id"] for p in rev.pendentes_envio(conn, 10)] == ["imp-a", "imp-b"]
    rev.marcar_tentada(conn, "imp-a", 1, em=AGORA + timedelta(seconds=2))
    assert [p["import_id"] for p in rev.pendentes_envio(conn, 1)] == ["imp-b"]


def test_marcar_tentada_atualiza_a_hora_a_cada_tentativa(conn):
    rev.garantir_revisao(conn, "imp-a", _corpo(), "sha256:a", agora=AGORA)
    rev.garantir_revisao(conn, "imp-b", _corpo(), "sha256:b", agora=AGORA)
    rev.marcar_tentada(conn, "imp-a", 1, em=AGORA)
    rev.marcar_tentada(conn, "imp-b", 1, em=AGORA + timedelta(seconds=1))
    rev.marcar_tentada(conn, "imp-a", 1, em=AGORA + timedelta(seconds=2))
    assert [p["import_id"] for p in rev.pendentes_envio(conn, 10)] == ["imp-b", "imp-a"]


def test_retiradas_pendentes_as_presas_vao_para_o_fim(conn):
    for imp in ("imp-a", "imp-b"):
        rev.garantir_revisao(conn, imp, _corpo(), "sha256:a", agora=AGORA)
        rev.marcar_tentada(conn, imp, 1, em=AGORA)
        rev.retirar(conn, imp, "cancelado", em=AGORA)
    assert [p["import_id"] for p in rev.retiradas_pendentes(conn, 1)] == ["imp-a"]
    rev.marcar_retirada_presa(conn, "imp-a", 1, em=AGORA)
    assert [p["import_id"] for p in rev.retiradas_pendentes(conn, 1)] == ["imp-b"]
    assert [p["import_id"] for p in rev.retiradas_pendentes(conn, 10)] == ["imp-b", "imp-a"]
    assert rev.contar_retiradas_presas(conn) == 1


def test_retirada_presa_mais_antiga_volta_primeiro_e_sucesso_solta(conn):
    for imp in ("imp-a", "imp-b", "imp-c"):
        rev.garantir_revisao(conn, imp, _corpo(), "sha256:a", agora=AGORA)
        rev.marcar_tentada(conn, imp, 1, em=AGORA)
        rev.retirar(conn, imp, "cancelado", em=AGORA)
    rev.marcar_retirada_presa(conn, "imp-b", 1, em=AGORA)
    rev.marcar_retirada_presa(conn, "imp-a", 1, em=AGORA + timedelta(seconds=1))
    assert [p["import_id"] for p in rev.retiradas_pendentes(conn, 10)] == [
        "imp-c",
        "imp-b",
        "imp-a",
    ]
    rev.marcar_retirada_enviada(conn, "imp-b", 1, em=AGORA)
    assert rev.contar_retiradas_presas(conn) == 1
    assert rev.soltar_retiradas_presas(conn) == 1
    assert rev.contar_retiradas_presas(conn) == 0


def test_tentativas_somam_e_zeram_por_revisao(conn):
    rev.garantir_revisao(conn, "imp-a", _corpo(), "sha256:a", agora=AGORA)
    assert rev.somar_tentativa(conn, "imp-a", 1) == 1
    assert rev.somar_tentativa(conn, "imp-a", 1) == 2
    rev.zerar_tentativas(conn, "imp-a", 1)
    assert rev.somar_tentativa(conn, "imp-a", 1) == 1


@pytest.fixture
def avisos():
    from app.utils.logger import logger

    msgs: list[str] = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    yield msgs
    logger.remove(sink)


def test_decisao_de_revisao_acima_da_local_avisa_uma_vez(conn, avisos):
    rev.garantir_revisao(conn, IMP, _corpo(), "sha256:a", agora=AGORA)
    assert rev.gravar_decisao(conn, _decisao("d3", 3), agora=AGORA) == "descartada"
    assert rev.gravar_decisao(conn, _decisao("d3", 3), agora=DEPOIS) == "descartada"
    linhas = [m for m in avisos if "backup" in m]
    assert len(linhas) == 1
    assert IMP in linhas[0] and "3" in linhas[0] and "1" in linhas[0]


def test_decisao_de_revisao_velha_nao_avisa_restore(conn, avisos):
    rev.garantir_revisao(conn, IMP, _corpo("a"), "sha256:a", agora=AGORA)
    rev.garantir_revisao(conn, IMP, _corpo("b"), "sha256:b", agora=AGORA)
    assert rev.gravar_decisao(conn, _decisao("d1", 1), agora=AGORA) == "descartada"
    assert [m for m in avisos if "backup" in m] == []


def test_banco_antigo_ganha_tentativas_e_presa_sem_perder_revisao(tmp_path, monkeypatch):
    """Migração aditiva: preco_revisao sem as colunas novas ganha as duas e
    mantém as linhas."""
    import sqlite3

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    velho = sqlite3.connect(router.env_db_path("mm"))
    velho.executescript(
        """
        CREATE TABLE preco_revisao (
            import_id TEXT NOT NULL, revisao INTEGER NOT NULL, conteudo_hash TEXT NOT NULL,
            corpo_json TEXT NOT NULL, situacao TEXT NOT NULL, tentada_em TEXT,
            enviada_em TEXT, resposta_json TEXT, erro_definitivo TEXT, retirada_em TEXT,
            retirada_motivo TEXT, retirada_enviada_em TEXT, bloqueio_local TEXT,
            criado_em TEXT NOT NULL, PRIMARY KEY (import_id, revisao));
        INSERT INTO preco_revisao (import_id, revisao, conteudo_hash, corpo_json, situacao,
            criado_em) VALUES ('imp-a', 1, 'h', '{}', 'pendente_envio', '2026-10-18');
        """
    )
    velho.commit()
    velho.close()
    with router.env_connect("mm") as c:
        linha = dict(c.execute("SELECT * FROM preco_revisao").fetchone())
    assert linha["import_id"] == "imp-a"
    assert linha["tentativas"] == 0
    assert linha["retirada_presa_em"] is None
