"""Decisão pura por item: produto só por chave exata (spec §2, §5.1)."""

from __future__ import annotations

from app.erp.produto_chave import MOTIVO_CHAVES_DISCORDAM, Candidato, decidir


def _c(seq: int, *, bloqueado: bool = False, cod: str | None = None) -> Candidato:
    return Candidato(
        seq=seq,
        descricao=f"PRODUTO {seq}",
        codprod_altern=cod,
        bloqueado=bloqueado,
        unidade="PAR",
        preco_venda=10.0,
    )


def _decidir(**kw):
    base = {
        "codigo": None,
        "ean": None,
        "por_ean": [],
        "por_codigo": [],
        "vinculo_seq": None,
        "vinculo_candidato": None,
    }
    base.update(kw)
    return decidir(**base)


def test_ean_unico_ativo_ok():
    d = _decidir(ean="7891", por_ean=[_c(10)])
    assert d.estado == "ok"
    assert d.fire_seq == 10
    assert d.fonte == "ean"
    assert d.motivo == ""


def test_ean_em_dois_ativos_ambiguo():
    d = _decidir(ean="7891", por_ean=[_c(10), _c(11)])
    assert d.estado == "ambiguo"
    assert d.fire_seq is None
    assert d.fonte is None
    assert [c.seq for c in d.candidatos] == [10, 11]
    assert d.motivo == "O EAN 7891 está em 2 produtos ativos."


def test_codigo_em_dois_ativos_ambiguo_motivo():
    d = _decidir(codigo="KG02PR", por_codigo=[_c(10), _c(11), _c(12, bloqueado=True)])
    assert d.estado == "ambiguo"
    assert [c.seq for c in d.candidatos] == [10, 11]
    assert d.motivo == "O código KG02PR está em 2 produtos ativos."


def test_ean_so_bloqueado():
    d = _decidir(ean="7891", por_ean=[_c(10, bloqueado=True)])
    assert d.estado == "produto_bloqueado"
    assert d.fire_seq is None
    assert [c.seq for c in d.candidatos] == [10]
    assert d.motivo == "O produto 10 está bloqueado no Fire."


def test_um_ativo_e_um_bloqueado_casa_o_ativo():
    d = _decidir(ean="7891", por_ean=[_c(10, bloqueado=True), _c(11)])
    assert (d.estado, d.fire_seq, d.fonte) == ("ok", 11, "ean")


def test_ean_ambiguo_resolvido_por_depara():
    d = _decidir(
        ean="7891",
        por_ean=[_c(10), _c(11)],
        vinculo_seq=11,
        vinculo_candidato=_c(11),
    )
    assert d.estado == "ok"
    assert d.fire_seq == 11
    assert d.fonte == "depara"
    assert [c.seq for c in d.candidatos] == [11]


def test_chaves_discordam():
    d = _decidir(ean="7891", codigo="ABC", por_ean=[_c(10)], por_codigo=[_c(11)])
    assert d.estado == "chaves_discordam"
    assert d.fire_seq is None
    assert [c.seq for c in d.candidatos] == [10, 11]
    assert d.motivo == (
        "As chaves do item (EAN, código ou vínculo salvo) apontam para produtos diferentes."
    )


def test_depara_discorda_do_ean():
    d = _decidir(ean="7891", por_ean=[_c(10)], vinculo_seq=11, vinculo_candidato=_c(11))
    assert d.estado == "chaves_discordam"
    assert [c.seq for c in d.candidatos] == [10, 11]


def test_ean_e_codigo_concordam_fonte_ean():
    d = _decidir(ean="7891", codigo="ABC", por_ean=[_c(10)], por_codigo=[_c(10)])
    assert (d.estado, d.fire_seq, d.fonte) == ("ok", 10, "ean")


def test_so_codigo_fonte_codprod_altern():
    d = _decidir(codigo="ABC", por_codigo=[_c(10)])
    assert (d.estado, d.fire_seq, d.fonte) == ("ok", 10, "codprod_altern")


def test_vinculo_para_seq_inexistente_invalido():
    d = _decidir(codigo="ABC", vinculo_seq=99, vinculo_candidato=None)
    assert d.estado == "vinculo_invalido"
    assert d.fire_seq is None
    assert d.candidatos == ()
    assert d.motivo == (
        "O vínculo salvo aponta para o produto 99, que não existe mais ou está bloqueado."
    )


def test_vinculo_para_bloqueado_invalido():
    d = _decidir(codigo="ABC", vinculo_seq=99, vinculo_candidato=_c(99, bloqueado=True))
    assert d.estado == "vinculo_invalido"
    assert d.fire_seq is None
    assert [c.seq for c in d.candidatos] == [99]


def test_vinculo_invalido_nao_cai_no_codigo_sem_avisar():
    """R7(a): código casa 1 produto, mas o vínculo é inválido: vínculo inválido vence."""
    d = _decidir(codigo="ABC", por_codigo=[_c(10)], vinculo_seq=99, vinculo_candidato=None)
    assert (d.estado, d.fire_seq, d.fonte) == ("vinculo_invalido", None, None)


def test_vinculo_bloqueado_vence_ean_e_codigo_que_concordam():
    d = _decidir(
        ean="7891",
        codigo="ABC",
        por_ean=[_c(10)],
        por_codigo=[_c(10)],
        vinculo_seq=10,
        vinculo_candidato=_c(10, bloqueado=True),
    )
    assert d.estado == "vinculo_invalido"
    assert [c.seq for c in d.candidatos] == [10]


def test_ean_ambiguo_resolvido_por_codigo_entre_os_candidatos():
    d = _decidir(ean="7891", codigo="ABC", por_ean=[_c(10), _c(11)], por_codigo=[_c(10)])
    assert (d.estado, d.fire_seq, d.fonte) == ("ok", 10, "codprod_altern")
    assert [c.seq for c in d.candidatos] == [10]


def test_ean_ambiguo_e_codigo_fora_dos_candidatos_discordam():
    d = _decidir(ean="7891", codigo="ABC", por_ean=[_c(10), _c(11)], por_codigo=[_c(12)])
    assert d.estado == "chaves_discordam"
    assert d.fire_seq is None
    assert [c.seq for c in d.candidatos] == [10, 11, 12]
    assert d.motivo == MOTIVO_CHAVES_DISCORDAM


def test_ean_so_bloqueado_e_codigo_ativo_discordam():
    d = _decidir(ean="7891", codigo="ABC", por_ean=[_c(99, bloqueado=True)], por_codigo=[_c(10)])
    assert d.estado == "chaves_discordam"
    assert [c.seq for c in d.candidatos] == [99, 10]


def test_ean_ambiguo_e_vinculo_fora_dos_candidatos_discordam():
    d = _decidir(ean="7891", por_ean=[_c(10), _c(11)], vinculo_seq=12, vinculo_candidato=_c(12))
    assert d.estado == "chaves_discordam"
    assert [c.seq for c in d.candidatos] == [10, 11, 12]


def test_ok_traz_o_candidato_decidido():
    d = _decidir(codigo="ABC", por_codigo=[_c(10, cod="ABC")])
    assert d.estado == "ok"
    assert d.candidatos == (_c(10, cod="ABC"),)


def test_prioridade_dos_problemas():
    d = _decidir(
        ean="7891",
        codigo="ABC",
        por_ean=[_c(10), _c(11)],
        por_codigo=[_c(12, bloqueado=True)],
        vinculo_seq=99,
        vinculo_candidato=None,
    )
    assert d.estado == "vinculo_invalido"
    d = _decidir(
        ean="7891",
        codigo="ABC",
        por_ean=[_c(12, bloqueado=True)],
        por_codigo=[_c(10), _c(11)],
    )
    assert d.estado == "ambiguo"
    assert d.motivo == "O código ABC está em 2 produtos ativos."


def test_nada_a_identificar_motivo():
    d = _decidir(codigo="KG02PR")
    assert d.estado == "a_identificar"
    assert d.fire_seq is None
    assert d.candidatos == ()
    assert d.motivo == "O cliente mandou o código KG02PR, que não está cadastrado no Fire."


def test_sem_codigo_a_identificar_motivo():
    d = _decidir(ean="7891")
    assert d.estado == "a_identificar"
    assert d.motivo == "O item veio sem código e sem EAN cadastrado no Fire."
