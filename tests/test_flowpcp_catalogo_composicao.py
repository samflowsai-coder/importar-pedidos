import pytest

from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp import catalogo_composicao as cc


def _p(seq, tipo="simples", alt=None, nome=None, ativo=True, tem_composicao=None):
    # tem_composicao None = o que o extract faz com IS_KIT: kit/pack têm, par não
    if tem_composicao is None:
        tem_composicao = tipo != "simples"
    return ProdutoFireDTO(
        seq, seq, nome or f"P{seq}", "PARES", None, ativo, tipo,
        codigo_alternativo=alt, tem_composicao=tem_composicao,
    )


# ── interruptor e exclusão guardada ─────────────────────────────────────


@pytest.mark.parametrize(
    "valor,slug,ligado",
    [("mm", "mm", True), (" MM , nasmar ", "nasmar", True), ("mm", "nasmar", False),
     ("", "mm", False), ("1", "mm", False)],
)
def test_interruptor_por_slug(monkeypatch, valor, slug, ligado):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", valor)
    assert cc.envio_ligado(slug) is ligado


def test_interruptor_ausente_e_desligado(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", raising=False)
    assert cc.envio_ligado("mm") is False


def test_excluir_guardado(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", " 3174, 03381 ,")
    assert cc.excluir_guardado("mm") == frozenset({"3174", "3381"})
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM")
    assert cc.excluir_guardado("mm") == frozenset()


def test_excluir_guardado_slug_com_hifen(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM_AMER", "1")
    assert cc.excluir_guardado("mm-amer") == frozenset({"1"})


@pytest.mark.parametrize("valor", ["3174;3381", "abc", "٣١", "12345678901"])
def test_excluir_guardado_invalido_falha_fechado(monkeypatch, valor):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", valor)
    with pytest.raises(ValueError, match="PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM"):
        cc.excluir_guardado("mm")


# ── codigoMes ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tipo,ativo,alt,esperado",
    [
        ("simples", True, "8366", "8366"),
        ("simples", True, " 113647 ", "113647"),
        ("simples", True, "123456789", "123456789"),
        ("simples", True, "1234567890", None),  # 10 dígitos: não cabe no integer do Flow
        ("simples", True, "986388014917", None),  # código de cliente
        ("simples", True, "SAM´S-BP34", None),
        ("simples", True, "", None),
        ("simples", True, None, None),
        ("simples", True, "0100", "0100"),  # sozinho: vai o valor cru, o Flow que normaliza
        ("simples", True, "0", None),  # zero não é código
        ("simples", True, "000", None),
        ("simples", True, "٣١", None),  # dígito não ASCII
        ("simples", False, "8366", None),  # par inativo
        ("kit", True, "8366", None),
        ("pack", True, "8366", None),
    ],
)
def test_codigo_mes(tipo, ativo, alt, esperado):
    assert cc.codigo_mes(tipo, ativo, alt) == esperado


def test_codigo_mes_repetido_vira_null_nos_dois():
    dtos = [_p("3759", alt="113649"), _p("3174", alt="113649"), _p("3306", alt="8366")]
    codigos, repetidos = cc.codigos_mes(dtos)
    assert codigos == {"3759": None, "3174": None, "3306": "8366"}
    assert repetidos == {"113649": ["3174", "3759"]}


def test_codigo_mes_repetido_pelo_inteiro_zero_a_esquerda():
    # o Flow lê ^\d{1,9}$ como inteiro: "0100" e "100" são o mesmo vínculo
    dtos = [_p("1", alt="0100"), _p("2", alt="100"), _p("3", alt="8366")]
    codigos, repetidos = cc.codigos_mes(dtos)
    assert codigos == {"1": None, "2": None, "3": "8366"}
    assert repetidos == {"100": ["1", "2"]}


# ── composição ──────────────────────────────────────────────────────────


def test_c3_kit_sem_linha_vai_sem_a_chave_e_par_sem_linha_vai_vazio():
    dtos = [_p("2544", "kit"), _p("3306"), _p("3364"), _p("2133", "kit"), _p("2645", "pack")]
    r = cc.montar_composicao(dtos, {"2544": [("3306", 2.0), ("3364", 4.0)]})
    assert r.leitura_ok is True
    assert r.por_seq["2544"] == [("3306", 2.0), ("3364", 4.0)]
    assert r.por_seq["3306"] == [] and r.por_seq["3364"] == []
    assert "2133" not in r.por_seq and "2645" not in r.por_seq
    assert r.sem_linha == ("2133", "2645")
    assert r.omitidos == {}


@pytest.mark.parametrize(
    "lida,motivo",
    [
        (None, "leitura de PRODUTOS_KIT falhou"),
        ({}, "leitura de PRODUTOS_KIT voltou vazia"),
        ({"9999": [("3306", 1.0)]}, "leitura incoerente: nenhum kit do catálogo está nela"),
    ],
)
def test_leitura_com_falha_nenhum_item_leva_a_chave(lida, motivo):
    r = cc.montar_composicao([_p("2544", "kit"), _p("3306")], lida)
    assert r.leitura_ok is False and r.motivo_falha == motivo
    assert r.por_seq == {} and r.omitidos == {} and r.sem_linha == ()


def test_catalogo_so_de_pares_com_leitura_vazia_e_falha_tambem():
    # sem kit nenhum no catálogo, leitura vazia ainda é suspeita: não afirmar [] em massa
    r = cc.montar_composicao([_p("3306")], {})
    assert r.leitura_ok is False


def test_filho_fora_do_envio_omite_a_chave_do_pai():
    # 3174 saiu pela exclusão; o kit 3381 não pode afirmar composição com par ausente
    dtos = [_p("3381", "kit"), _p("3170")]
    r = cc.montar_composicao(dtos, {"3381": [("3174", 1.0), ("3170", 1.0)]})
    assert "3381" not in r.por_seq
    assert r.omitidos["3381"] == "filho fora do envio: 3174"


@pytest.mark.parametrize(
    "linhas,motivo",
    [
        ([("3306", 1.0), ("3306", 1.0)], "filho repetido: 3306"),
        ([("3306", 0.0)], "quantidade fora de 0 < q <= 100000: 3306"),
        ([("3306", -1.0)], "quantidade fora de 0 < q <= 100000: 3306"),
        ([("3306", 100001.0)], "quantidade fora de 0 < q <= 100000: 3306"),
        ([("2544", 1.0)], "kit dentro dele mesmo"),
        ([(None, 1.0)], "linha com filho ou quantidade nulos"),
        ([("3306", None)], "linha com filho ou quantidade nulos"),
    ],
)
def test_composicao_invalida_omite_com_motivo(linhas, motivo):
    r = cc.montar_composicao([_p("2544", "kit"), _p("3306")], {"2544": linhas})
    assert "2544" not in r.por_seq and r.omitidos["2544"] == motivo


def test_c3_real_kit_sem_tem_composicao_e_sem_linha_fica_sem_a_chave():
    # kit por nome/unidade (IS_KIT falso, sem linhas) convive com um kit IS_KIT lido
    dtos = [_p("2544", "kit"), _p("3306"), _p("2133", "kit", tem_composicao=False)]
    r = cc.montar_composicao(dtos, {"2544": [("3306", 1.0)]})
    assert r.leitura_ok is True
    assert "2133" not in r.por_seq and r.sem_linha == ("2133",)


def test_exatamente_50_filhos_e_aceito():
    filhos = [_p(str(4000 + i)) for i in range(50)]
    lida = {"2544": [(f.fire_produto_id, 1.0) for f in filhos]}
    r = cc.montar_composicao([_p("2544", "kit"), *filhos], lida)
    assert len(r.por_seq["2544"]) == 50 and r.omitidos == {}


def test_quantidade_100000_e_aceita():
    r = cc.montar_composicao([_p("2544", "kit"), _p("3306")], {"2544": [("3306", 100000.0)]})
    assert r.por_seq["2544"] == [("3306", 100000.0)] and r.omitidos == {}


def test_so_kits_por_nome_com_leitura_nao_vazia_nao_e_falha():
    dtos = [_p("2133", "kit", tem_composicao=False), _p("3306")]
    r = cc.montar_composicao(dtos, {"9999": [("3306", 1.0)]})
    assert r.leitura_ok is True and r.sem_linha == ("2133",)


def test_mais_de_50_filhos_omite():
    filhos = [_p(str(4000 + i)) for i in range(51)]
    lida = {"2544": [(f.fire_produto_id, 1.0) for f in filhos]}
    r = cc.montar_composicao([_p("2544", "kit"), *filhos], lida)
    assert r.omitidos["2544"] == "mais de 50 filhos (51)"


@pytest.mark.parametrize(
    "nome,linhas,divergente",
    [
        ("KIT C/5 SAM'S LISTRADO", [("3306", 1.0), ("3364", 4.0)], False),
        ("KIT C/5 SAM'S LISTRADO", [("3306", 1.0), ("3364", 12.0)], True),
        ("KIT 6 PRETO", [("3306", 6.0)], False),
        ("PACK C/36 KINGS", [("3306", 3.0)], True),
        ("KIT POLO MATCH", [("3306", 3.0)], False),  # nome sem quantidade: não confere
    ],
)
def test_quantidade_contra_o_nome_so_avisa(nome, linhas, divergente):
    r = cc.montar_composicao([_p("2544", "kit", nome=nome), _p("3306"), _p("3364")], {"2544": linhas})
    assert r.por_seq["2544"] == linhas  # nunca omite por isso
    assert ("2544" in r.divergencia_nome) is divergente


def test_resumo_conta_tudo():
    dtos = [_p("2544", "kit"), _p("2133", "kit"), _p("3306", alt="8366"), _p("3364")]
    comp = cc.montar_composicao(dtos, {"2544": [("3306", 2.0)]})
    codigos, repetidos = cc.codigos_mes(dtos)
    assert cc.resumo(comp, codigos, repetidos) == {
        "leitura_ok": True,
        "motivo_falha": None,
        "com_lista": 1,
        "pares_vazios": 2,
        "kits_sem_composicao": 1,
        "kits_sem_composicao_seqs": ["2133"],
        "omitidos": {},
        "divergencia_nome": {},
        "codigo_mes": 1,
        "codigo_mes_repetido": {},
    }
