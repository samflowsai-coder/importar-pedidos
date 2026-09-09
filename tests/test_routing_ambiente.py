from __future__ import annotations

from app.models.order import Order, OrderHeader, OrderItem
from app.routing import ambiente
from app.routing.ambiente import Deps
from app.routing.historico import HistoricoAmbiente

NASMAR = "34513679000134"
MM = "35394871000111"
CLIENTE = "11222333000181"


def _pedido(*, supplier=None, cliente=CLIENTE, entregas=()):
    return Order(
        header=OrderHeader(
            order_number="4711", customer_cnpj=cliente, customer_name="DAJU", supplier_cnpj=supplier
        ),
        items=[OrderItem(description="Meia", quantity=1, delivery_cnpj=c) for c in entregas]
        or [OrderItem(description="Meia", quantity=1)],
    )


def _deps(*, por_cnpj=None, hist=(), memoria=None, historico_amplo=None):
    return Deps(
        env_por_cnpj=lambda c: (por_cnpj or {}).get(c),
        historico=lambda cnpjs: hist,  # noqa: ARG005
        memoria=lambda c: memoria,  # noqa: ARG005
        historico_amplo=historico_amplo,
    )


def test_documento_resolve():
    d = ambiente.ambiente_para(_pedido(supplier=NASMAR), _deps(por_cnpj={NASMAR: "nasmar"}))
    assert d.env_slug == "nasmar"
    assert d.degrau == "documento"
    assert "34.513.679/0001-34" in d.explicacao or NASMAR in d.explicacao


def test_documento_vence_historico_e_memoria():
    """A regra que torna o caso Centauro impossivel por construcao."""
    d = ambiente.ambiente_para(
        _pedido(supplier=MM),
        _deps(
            por_cnpj={MM: "americanense", NASMAR: "nasmar"},
            hist=(HistoricoAmbiente("nasmar", "Nasmar", 40, "2026-08-01"),),
            memoria="nasmar",
        ),
    )
    assert d.env_slug == "americanense"
    assert d.degrau == "documento"
    assert d.divergiu_de == "nasmar"


def test_historico_vence_memoria():
    d = ambiente.ambiente_para(
        _pedido(),
        _deps(
            hist=(HistoricoAmbiente("nasmar", "Nasmar", 28, "2026-08-24"),),
            memoria="americanense",
        ),
    )
    assert d.env_slug == "nasmar"
    assert d.degrau == "historico"
    assert d.divergiu_de == "americanense"


def test_memoria_responde_quando_documento_e_historico_calam():
    d = ambiente.ambiente_para(_pedido(), _deps(memoria="nasmar"))
    assert d.env_slug == "nasmar"
    assert d.degrau == "memoria"
    assert d.divergiu_de is None


def test_sem_nada_pergunta_e_nunca_devolve_default():
    d = ambiente.ambiente_para(_pedido(), _deps())
    assert d.env_slug is None
    assert d.degrau == "perguntar"


def test_historico_ambiguo_cai_para_o_degrau_seguinte():
    """Cliente nos dois bancos: 1 em 277. Nao vota no maior volume."""
    d = ambiente.ambiente_para(
        _pedido(),
        _deps(
            hist=(
                HistoricoAmbiente("nasmar", "Nasmar", 28, "2026-08-24"),
                HistoricoAmbiente("americanense", "MM", 6, "2026-05-28"),
            ),
            memoria="americanense",
        ),
    )
    assert d.degrau == "memoria"
    assert d.env_slug == "americanense"


def test_historico_ambiguo_sem_memoria_pergunta():
    d = ambiente.ambiente_para(
        _pedido(),
        _deps(
            hist=(
                HistoricoAmbiente("nasmar", "Nasmar", 28, "2026-08-24"),
                HistoricoAmbiente("americanense", "MM", 6, "2026-05-28"),
            )
        ),
    )
    assert d.env_slug is None
    assert d.degrau == "perguntar"
    assert len(d.historico) == 2  # a UI mostra os dois, mesmo sem resolver
    assert "mais de uma empresa" in d.explicacao


def test_supplier_cnpj_desconhecido_nao_resolve():
    """CNPJ de fornecedor que nao e de nenhum ambiente: cai, nao inventa."""
    d = ambiente.ambiente_para(
        _pedido(supplier="99999999000199"), _deps(por_cnpj={NASMAR: "nasmar"})
    )
    assert d.degrau == "perguntar"


def test_supplier_cnpj_malformado_nao_derruba():
    d = ambiente.ambiente_para(_pedido(supplier="abc"), _deps())
    assert d.degrau == "perguntar"


def test_cnpjs_do_pedido_junta_cliente_e_lojas_sem_repetir():
    order = _pedido(
        cliente="05055599002985", entregas=("05.055.599/0026-32", "05055599002985", None)
    )
    assert ambiente.cnpjs_do_pedido(order) == ["05055599002985", "05055599002632"]


def test_pedido_sem_cnpj_nenhum_pergunta():
    order = Order(
        header=OrderHeader(order_number="X"), items=[OrderItem(description="Meia", quantity=1)]
    )
    d = ambiente.ambiente_para(order, _deps())
    assert d.degrau == "perguntar"
    assert d.env_slug is None
    assert "não traz CNPJ de cliente nem de fornecedor" in d.explicacao
    assert "não tem histórico" not in d.explicacao


# --- Correção 1: indisponibilidade do Fire é diferente de cliente novo -----


def test_historico_indisponivel_explica_diferente_de_cliente_novo():
    """Operador que recebe 'escolha a empresa' precisa saber que o motivo foi
    o Firebird mudo, nao que o cliente e novo — sao acoes diferentes."""
    d = ambiente.ambiente_para(
        _pedido(),
        _deps(hist=(HistoricoAmbiente("nasmar", "Nasmar", 0, None, indisponivel=True),)),
    )
    assert d.degrau == "perguntar"
    assert d.env_slug is None
    assert "Nasmar" in d.explicacao
    assert "não respondeu" in d.explicacao
    assert "não tem histórico" not in d.explicacao


def test_historico_indisponivel_dois_ambientes_pluraliza():
    d = ambiente.ambiente_para(
        _pedido(),
        _deps(
            hist=(
                HistoricoAmbiente("nasmar", "Nasmar", 0, None, indisponivel=True),
                HistoricoAmbiente("americanense", "MM", 0, None, indisponivel=True),
            )
        ),
    )
    assert d.degrau == "perguntar"
    assert "Nasmar" in d.explicacao and "MM" in d.explicacao
    assert "não responderam" in d.explicacao


def test_cliente_novo_sem_indisponibilidade_explica_sem_historico():
    d = ambiente.ambiente_para(_pedido(), _deps())
    assert d.degrau == "perguntar"
    assert "não tem histórico" in d.explicacao
    assert "não respond" not in d.explicacao


# --- Correção 2: divergência fora da janela de 12 meses, na explicação -----
#
# `historico_amplo` é injetado via `Deps` (não via monkeypatch de função de
# módulo) — é exatamente essa mudança que fecha o Achado 1 da revisão: sem
# `historico_amplo` explícito, `_deps()` monta `Deps(historico_amplo=None)`
# (o mesmo default de `Deps`), e `ambiente_para` não faz round-trip nenhum
# ao Firebird. Antes desta correção, o caminho sem override caía direto em
# `historico.consultar(meses=24)` de verdade — foi assim que um teste
# unitário chegou a abrir conexão com o Firebird de PRODUÇÃO do cliente.


def test_explicar_fora_da_janela_sem_historico_amplo_nao_chama_nada():
    """O caso do Achado 1: sem `historico_amplo` (o default), zero I/O — a
    função nem tenta consultar nada, só devolve "" na hora."""
    resultado = ambiente._explicar_fora_da_janela(
        [CLIENTE], (HistoricoAmbiente("nasmar", "Nasmar", 28, "2026-08-24"),), None
    )
    assert resultado == ""


def test_deps_historico_amplo_e_opcional_construcao_com_tres_campos_continua_valida():
    """Aditivo, não disruptivo: quem monta Deps com os 3 campos originais
    (Tasks 10/11, e os testes delas) continua funcionando sem mudar nada."""
    d = Deps(env_por_cnpj=lambda c: None, historico=lambda c: (), memoria=lambda c: None)  # noqa: ARG005
    assert d.historico_amplo is None


def test_historico_resolvido_anexa_divergencia_fora_da_janela():
    """Caso Beira Rio: resolve limpo pra nasmar na janela de 12 meses, mas o
    Portal avisa que o cliente tambem comprou da MM ate 05/2025."""
    chamadas = []

    def fake_ampla(cnpjs):
        chamadas.append(list(cnpjs))
        return (
            HistoricoAmbiente("nasmar", "Nasmar", 28, "2026-08-24"),
            HistoricoAmbiente("americanense", "MM", 6, "2025-05-20"),
        )

    d = ambiente.ambiente_para(
        _pedido(),
        _deps(
            hist=(HistoricoAmbiente("nasmar", "Nasmar", 28, "2026-08-24"),),
            historico_amplo=fake_ampla,
        ),
    )
    assert d.env_slug == "nasmar"
    assert d.degrau == "historico"
    assert "atenção" in d.explicacao
    assert "MM" in d.explicacao
    assert "6" in d.explicacao
    assert "2025-05-20" in d.explicacao
    assert chamadas == [[CLIENTE]]


def test_historico_resolvido_sem_divergencia_fora_da_janela_nao_menciona_atencao():
    d = ambiente.ambiente_para(
        _pedido(),
        _deps(
            hist=(HistoricoAmbiente("nasmar", "Nasmar", 28, "2026-08-24"),),
            historico_amplo=lambda cnpjs: (  # noqa: ARG005
                HistoricoAmbiente("nasmar", "Nasmar", 28, "2026-08-24"),
            ),
        ),
    )
    assert d.degrau == "historico"
    assert "atenção" not in d.explicacao


def test_segunda_consulta_com_erro_nao_derruba_o_pedido():
    def explode(cnpjs):  # noqa: ARG001
        raise OSError("vpn down")

    d = ambiente.ambiente_para(
        _pedido(),
        _deps(
            hist=(HistoricoAmbiente("nasmar", "Nasmar", 28, "2026-08-24"),),
            historico_amplo=explode,
        ),
    )
    assert d.env_slug == "nasmar"
    assert d.degrau == "historico"
    assert "atenção" not in d.explicacao


def test_segunda_consulta_so_roda_quando_historico_resolve():
    """Nao gasta a consulta ampla quando quem resolveu foi outro degrau."""
    chamou = False

    def espiao(cnpjs):  # noqa: ARG001
        nonlocal chamou
        chamou = True
        return ()

    # Degrau 1 resolve — historico nem roda.
    ambiente.ambiente_para(
        _pedido(supplier=NASMAR),
        _deps(por_cnpj={NASMAR: "nasmar"}, historico_amplo=espiao),
    )
    assert chamou is False

    # Degrau 2 nao resolve (ambiguo), degrau 3 resolve.
    ambiente.ambiente_para(
        _pedido(),
        _deps(
            hist=(
                HistoricoAmbiente("nasmar", "Nasmar", 28, "2026-08-24"),
                HistoricoAmbiente("americanense", "MM", 6, "2026-05-28"),
            ),
            memoria="americanense",
            historico_amplo=espiao,
        ),
    )
    assert chamou is False

    # Ninguem resolve.
    ambiente.ambiente_para(_pedido(), _deps(historico_amplo=espiao))
    assert chamou is False


# --- Minor 4: "último em None" não pode vazar pro texto -------------------


def test_historico_sem_ultimo_em_nao_mostra_none():
    d = ambiente.ambiente_para(
        _pedido(),
        _deps(hist=(HistoricoAmbiente("nasmar", "Nasmar", 3, None),)),
    )
    assert d.degrau == "historico"
    assert "None" not in d.explicacao
    assert "último em" not in d.explicacao


def test_deps_padrao_monta_as_quatro_leituras_reais():
    deps = ambiente.deps_padrao()
    assert callable(deps.env_por_cnpj)
    assert callable(deps.historico)
    assert callable(deps.memoria)
    assert callable(deps.historico_amplo)
