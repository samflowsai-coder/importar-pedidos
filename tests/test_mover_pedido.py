"""Mover pedido em revisão para a outra empresa (`app/persistence/mover_pedido.py`
e `POST /api/imported/{id}/mover-empresa`).

O pedido some na origem e nasce no destino com o MESMO id; nada derivado do
Fire/empresa de origem viaja; pedido com rastro de saída (Fire, XLSX, Flow,
Gestor) não se move.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.persistence import environments_repo, roteamento_repo, router
from app.persistence import mover_pedido as mp
from app.web.server import app

IMPORT_ID = "imp-mover-1"


@pytest.fixture
def envs_mm_nasmar(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    router.reset_init_cache()
    with router.shared_connect():
        pass
    mm = environments_repo.create(
        slug="mm",
        name="MM Americanense",
        cnpj="35394871000111",
        watch_dir=str(tmp_path / "m_in"),
        output_dir=str(tmp_path / "m_out"),
        fb_path=str(tmp_path / "m.fdb"),
    )
    nasmar = environments_repo.create(
        slug="nasmar",
        name="Nasmar",
        cnpj="34513679000134",
        watch_dir=str(tmp_path / "n_in"),
        output_dir=str(tmp_path / "n_out"),
        fb_path=str(tmp_path / "n.fdb"),
    )
    return {"mm": mm, "nasmar": nasmar}


def _snapshot():
    from app.models.order import Order, OrderHeader, OrderItem

    return Order(
        header=OrderHeader(
            order_number="4711", customer_cnpj="11222333000181", customer_name="DAJU"
        ),
        items=[OrderItem(description="Meia Kit 3", quantity=10, unit_price=11.96)],
    ).model_dump(mode="json")


def _semear(env, iid=IMPORT_ID, **cols):
    """Pedido em revisão na empresa `env`, com 2 audits e 1 evento de ciclo de vida."""
    linha = {
        "id": iid,
        "environment_id": env["id"],
        "source_filename": "PEDIDO.pdf",
        "imported_at": "2026-10-20T10:00:00",
        "order_number": "4711",
        "customer_cnpj": "11222333000181",
        "customer_name": "DAJU",
        "snapshot_json": json.dumps(_snapshot()),
        "status": "success",
        "portal_status": "parsed",
        "trace_id": "trace-1",
        "file_sha256": "abc",
        "original_path": "/arquivo/original/PEDIDO.pdf",
    } | cols
    with router.env_connect(env["slug"]) as conn:
        conn.execute(
            f"INSERT INTO imports ({', '.join(linha)}) VALUES ({', '.join('?' * len(linha))})",  # noqa: S608
            tuple(linha.values()),
        )
        for ev in ("imported_to_portal", "produto_vinculo_criado"):
            conn.execute(
                "INSERT INTO audit_log (environment_id, import_id, event_type, detail_json,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (env["id"], iid, ev, json.dumps({"ev": ev}), "2026-10-20T10:00:01"),
            )
        conn.execute(
            "INSERT INTO order_lifecycle_events (environment_id, import_id, event_type, source,"
            " payload_json, trace_id, occurred_at, ingested_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                env["id"],
                iid,
                "imported",
                "portal",
                "{}",
                "trace-1",
                "2026-10-20T10:00:01",
                "2026-10-20T10:00:01",
            ),
        )


def _linha(slug, tabela="imports", coluna="id"):
    with router.env_connect(slug) as conn:
        return [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM {tabela} WHERE {coluna} = ? ORDER BY rowid",  # noqa: S608
                (IMPORT_ID,),
            ).fetchall()
        ]


def _mover(envs, de="mm", para="nasmar"):
    return mp.mover_import(IMPORT_ID, origem=envs[de], destino=envs[para], por="ana@mm")


def test_move_pedido_em_revisao_com_audit_e_eventos(envs_mm_nasmar):
    _semear(envs_mm_nasmar["mm"])

    assert _mover(envs_mm_nasmar) == "movido"

    assert _linha("mm") == []
    (dest,) = _linha("nasmar")
    assert dest["environment_id"] == envs_mm_nasmar["nasmar"]["id"]
    assert dest["portal_status"] == "parsed"
    assert dest["original_path"] == "/arquivo/original/PEDIDO.pdf"
    assert dest["snapshot_json"] == json.dumps(_snapshot())
    audit = _linha("nasmar", "audit_log", "import_id")
    assert [a["event_type"] for a in audit] == [
        "imported_to_portal",
        "produto_vinculo_criado",
        "movido_de_empresa",
    ]
    assert {a["environment_id"] for a in audit} == {envs_mm_nasmar["nasmar"]["id"]}
    assert json.loads(audit[-1]["detail_json"]) == {"de": "mm", "para": "nasmar", "por": "ana@mm"}
    (evento,) = _linha("nasmar", "order_lifecycle_events", "import_id")
    assert evento["event_type"] == "imported"
    assert evento["environment_id"] == envs_mm_nasmar["nasmar"]["id"]
    assert _linha("mm", "audit_log", "import_id") == []
    assert _linha("mm", "order_lifecycle_events", "import_id") == []


def test_move_limpa_dados_derivados_da_origem(envs_mm_nasmar):
    _semear(
        envs_mm_nasmar["mm"],
        check_json='{"available": true}',
        check_preco_json='{"available": true}',
        cliente_override_codigo=77,
        cliente_override_razao="DAJU LTDA",
        cliente_override_at="2026-10-20",
        cliente_override_by="ana@mm",
        cliente_override_cnpj="11222333000181",
        sem_preco_ack_by="ana@mm",
        sem_preco_ack_at="2026-10-20",
        sem_preco_ack_items="[]",
        produto_escolhido_json='{"0": {"fire_seq": 123}}',
        fire_status_last_seen="PEDIDO",
        fire_status_polled_at="2026-10-20",
        reconciled_at="2026-10-20",
        db_result_json='{"x": 1}',
    )
    with router.env_connect("mm") as conn:
        # Revisão de preço que nunca saiu para o Flow: some com o pedido.
        conn.execute(
            "INSERT INTO preco_revisao (import_id, revisao, conteudo_hash, corpo_json,"
            " situacao, criado_em) VALUES (?, 1, 'h', '{}', 'pendente_envio', '2026-10-20')",
            (IMPORT_ID,),
        )

    _mover(envs_mm_nasmar)

    (dest,) = _linha("nasmar")
    for col in (
        "check_json",
        "check_preco_json",
        "cliente_override_codigo",
        "cliente_override_razao",
        "cliente_override_at",
        "cliente_override_by",
        "cliente_override_cnpj",
        "sem_preco_ack_by",
        "sem_preco_ack_at",
        "sem_preco_ack_items",
        "produto_escolhido_json",
        "fire_status_last_seen",
        "fire_status_polled_at",
        "reconciled_at",
        "db_result_json",
    ):
        assert dest[col] is None, col
    for tabela in (
        "preco_revisao",
        "preco_revisao_piso",
        "preco_decisao",
        "erp_item_vinculo",
        "erp_xlsx_linha",
        "outbox",
        "audit_log",
        "order_lifecycle_events",
    ):
        assert _linha("mm", tabela, "import_id") == [], tabela
    assert _linha("nasmar", "preco_revisao", "import_id") == []


def _nada_mudou(envs):
    assert len(_linha("mm")) == 1
    assert _linha("nasmar") == []
    assert len(_linha("mm", "audit_log", "import_id")) == 2


def test_nao_move_pedido_com_fire_codigo(envs_mm_nasmar):
    _semear(envs_mm_nasmar["mm"], fire_codigo=5083)
    with pytest.raises(mp.PedidoNaoMovivel) as exc:
        _mover(envs_mm_nasmar)
    assert exc.value.motivo == "já foi para o Fire"
    _nada_mudou(envs_mm_nasmar)


def test_nao_move_pedido_exportado_xlsx(envs_mm_nasmar):
    _semear(envs_mm_nasmar["mm"], output_files_json='[{"path": "x.xlsx"}]')
    with pytest.raises(mp.PedidoNaoMovivel) as exc:
        _mover(envs_mm_nasmar)
    assert exc.value.motivo == "já foi exportado em XLSX"
    _nada_mudou(envs_mm_nasmar)


@pytest.mark.parametrize("output", [None, "", "[]"])
def test_output_vazio_nao_conta_como_exportado(envs_mm_nasmar, output):
    _semear(envs_mm_nasmar["mm"], output_files_json=output)
    assert _mover(envs_mm_nasmar) == "movido"


def test_nao_move_pedido_fora_de_revisao(envs_mm_nasmar):
    _semear(envs_mm_nasmar["mm"], portal_status="cancelled")
    with pytest.raises(mp.PedidoNaoMovivel) as exc:
        _mover(envs_mm_nasmar)
    assert exc.value.motivo == "não está em revisão"
    _nada_mudou(envs_mm_nasmar)


_RASTROS = {
    "erp_item_vinculo": (
        "INSERT INTO erp_item_vinculo (base, erp_item_codigo, erp_codigo, import_id,"
        " indice_original, origem, criado_em) VALUES ('mm', 1, 9, ?, 0, 'insert', 'x')",
        "já foi para o Fire",
    ),
    "erp_xlsx_linha": (
        "INSERT INTO erp_xlsx_linha (import_id, arquivo, linha, indice_original, gerado_em)"
        " VALUES (?, 'a.xlsx', 2, 0, 'x')",
        "já foi exportado em XLSX",
    ),
    "outbox": (
        "INSERT INTO outbox (environment_id, import_id, target, endpoint, payload_json,"
        " idempotency_key, created_at) VALUES ('e', ?, 'flowpcp', '/r', '{}', 'k', 'x')",
        "já foi enviado ao Flow ou ao Gestor",
    ),
    "preco_revisao_tentada": (
        "INSERT INTO preco_revisao (import_id, revisao, conteudo_hash, corpo_json, situacao,"
        " tentada_em, criado_em) VALUES (?, 1, 'h', '{}', 'pendente_envio', 'x', 'x')",
        "o preço já foi para aprovação no Flow",
    ),
    "preco_revisao_enviada": (
        "INSERT INTO preco_revisao (import_id, revisao, conteudo_hash, corpo_json, situacao,"
        " enviada_em, criado_em) VALUES (?, 1, 'h', '{}', 'enviada', 'x', 'x')",
        "o preço já foi para aprovação no Flow",
    ),
    "preco_revisao_piso": (
        "INSERT INTO preco_revisao_piso (import_id, piso) VALUES (?, 3)",
        "o preço já foi para aprovação no Flow",
    ),
    "preco_decisao": (
        "INSERT INTO preco_decisao (decisao_id, import_id, revisao, item_chave, resultado,"
        " decidido_em, recebida_em, confirmacao) VALUES ('d', ?, 1, 'i', 'r', 'x', 'x', 'aplicada')",
        "o preço já foi para aprovação no Flow",
    ),
}


@pytest.mark.parametrize("rastro", sorted(_RASTROS))
def test_nao_move_pedido_com_rastro_de_saida(envs_mm_nasmar, rastro):
    _semear(envs_mm_nasmar["mm"])
    sql, motivo = _RASTROS[rastro]
    with router.env_connect("mm") as conn:
        conn.execute(sql, (IMPORT_ID,))
    with pytest.raises(mp.PedidoNaoMovivel) as exc:
        _mover(envs_mm_nasmar)
    assert exc.value.motivo == motivo
    _nada_mudou(envs_mm_nasmar)


def test_nao_move_pedido_ja_enviado_ao_gestor(envs_mm_nasmar):
    _semear(envs_mm_nasmar["mm"], gestor_order_id="g-1")
    with pytest.raises(mp.PedidoNaoMovivel) as exc:
        _mover(envs_mm_nasmar)
    assert exc.value.motivo == "já foi enviado ao Flow ou ao Gestor"
    _nada_mudou(envs_mm_nasmar)


def test_mover_interrompido_conclui_sem_duplicar(envs_mm_nasmar, monkeypatch):
    _semear(envs_mm_nasmar["mm"])
    real = mp._apagar_na_origem

    def _falha(conn, import_id):  # noqa: ARG001
        raise RuntimeError("queda no meio")

    monkeypatch.setattr(mp, "_apagar_na_origem", _falha)
    with pytest.raises(RuntimeError):
        _mover(envs_mm_nasmar)
    # Interrompido: a cópia nasceu, a origem ficou inteira.
    assert len(_linha("mm")) == 1
    assert len(_linha("nasmar")) == 1

    monkeypatch.setattr(mp, "_apagar_na_origem", real)
    assert _mover(envs_mm_nasmar) == "concluido"

    assert _linha("mm") == []
    assert len(_linha("nasmar")) == 1
    audit = [a["event_type"] for a in _linha("nasmar", "audit_log", "import_id")]
    assert audit == ["imported_to_portal", "produto_vinculo_criado", "movido_de_empresa"]
    assert len(_linha("nasmar", "order_lifecycle_events", "import_id")) == 1


def test_mover_interrompido_e_origem_saiu_desfaz_a_copia(envs_mm_nasmar, monkeypatch):
    """Na janela do mover interrompido a origem foi para o Fire: a origem não
    se apaga, e a cópia do destino (ainda em revisão) some — nunca dois."""
    _semear(envs_mm_nasmar["mm"])
    real = mp._apagar_na_origem
    monkeypatch.setattr(mp, "_apagar_na_origem", lambda conn, import_id: 1 / 0)  # noqa: ARG005
    with pytest.raises(ZeroDivisionError):
        _mover(envs_mm_nasmar)
    monkeypatch.setattr(mp, "_apagar_na_origem", real)
    with router.env_connect("mm") as conn:
        conn.execute("UPDATE imports SET fire_codigo = 5083 WHERE id = ?", (IMPORT_ID,))

    with pytest.raises(mp.PedidoNaoMovivel) as exc:
        _mover(envs_mm_nasmar)

    assert exc.value.motivo == "já foi para o Fire"
    assert _linha("mm")[0]["fire_codigo"] == 5083
    assert _linha("nasmar") == []
    assert _linha("nasmar", "audit_log", "import_id") == []


def test_mover_de_novo_depois_de_movido_nao_faz_nada(envs_mm_nasmar):
    _semear(envs_mm_nasmar["mm"])
    assert _mover(envs_mm_nasmar) == "movido"
    assert _mover(envs_mm_nasmar) == "ja_movido"
    assert len(_linha("nasmar")) == 1
    assert len(_linha("nasmar", "audit_log", "import_id")) == 3


def test_mover_pedido_inexistente(envs_mm_nasmar):
    with pytest.raises(mp.PedidoNaoMovivel) as exc:
        _mover(envs_mm_nasmar)
    assert exc.value.motivo == "não encontrado"


# ── Rota ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(envs_mm_nasmar):
    return TestClient(app)


@pytest.fixture
def client_ligado(client):
    roteamento_repo.set_modo("ligado", por="t")
    return client


@pytest.fixture
def check_falso(monkeypatch):
    """`check_order` e `check_preco_para_gravar` sem Fire: anotam a empresa
    pedida e a ativa no contextvar."""
    from app.erp import preco_trava, product_check
    from app.persistence import context as env_context

    chamadas = {"check": [], "preco": []}

    def _check(order, *, env=None, escolhas=None):
        chamadas["check"].append((env["slug"], env_context.current_env_slug(), escolhas))
        return {"available": True, "de": env["slug"], "summary": {"items_total": len(order.items)}}

    def _preco(order, env, entry=None):  # noqa: ARG001
        chamadas["preco"].append((env["slug"], env_context.current_env_slug()))
        return {"available": True, "preco_fonte_slug": "mm"}

    monkeypatch.setattr(product_check, "check_order", _check)
    monkeypatch.setattr(preco_trava, "check_preco_para_gravar", _preco)
    return chamadas


def _post(c, destino="nasmar"):
    return c.post(f"/api/imported/{IMPORT_ID}/mover-empresa", json={"destino": destino})


def test_rota_409_em_desligado(client, envs_mm_nasmar):
    _semear(envs_mm_nasmar["mm"])
    roteamento_repo.set_modo("desligado", por="t")
    r = _post(client)
    assert r.status_code == 409
    assert len(_linha("mm")) == 1
    assert _linha("nasmar") == []


def test_rota_404_pedido_inexistente(client_ligado):
    assert _post(client_ligado).status_code == 404


def test_rota_422_destino_igual_ou_inativo(client_ligado, envs_mm_nasmar):
    _semear(envs_mm_nasmar["mm"])
    assert _post(client_ligado, "mm").status_code == 422
    assert _post(client_ligado, "nao-existe").status_code == 422
    environments_repo.soft_delete(envs_mm_nasmar["nasmar"]["id"])
    assert _post(client_ligado, "nasmar").status_code == 422
    assert len(_linha("mm")) == 1


def test_rota_409_com_motivo(client_ligado, envs_mm_nasmar):
    _semear(envs_mm_nasmar["mm"], fire_codigo=5083)
    r = _post(client_ligado)
    assert r.status_code == 409
    assert r.json()["motivo"] == "já foi para o Fire"


@pytest.mark.parametrize("modo", ["ligado", "observando"])
def test_rota_registra_movido_apos_importar_com_usuario(client, envs_mm_nasmar, check_falso, modo):
    from app.persistence import roteamento_decisao_repo

    roteamento_repo.set_modo(modo, por="t")
    _semear(envs_mm_nasmar["mm"])
    roteamento_decisao_repo.registrar(
        import_id=IMPORT_ID,
        modo=modo,
        degrau="regra",
        env_sugerido="mm",
        env_escolhido="mm",
        decidido_por="sistema:regra",
        motivo="x",
        acao="automatico",
    )

    r = _post(client)

    assert r.status_code == 200
    assert r.json() == {"import_id": IMPORT_ID, "de": "mm", "para": "nasmar"}
    ultima = roteamento_decisao_repo.ultima(IMPORT_ID)
    assert ultima["acao"] == "movido_apos_importar"
    assert ultima["decidido_por"] == "test@portal.local"
    assert ultima["env_sugerido"] == "mm"
    assert ultima["env_escolhido"] == "nasmar"
    assert ultima["degrau"] is None
    assert ultima["modo"] == modo
    assert ultima["motivo"] == "movido de mm para nasmar"


def test_rota_refaz_check_no_destino(client_ligado, envs_mm_nasmar, check_falso):
    _semear(
        envs_mm_nasmar["mm"],
        check_json='{"de": "mm"}',
        produto_escolhido_json='{"0": {"fire_seq": 9}}',
    )

    assert _post(client_ligado).status_code == 200

    # check de produto e de preço no Fire do DESTINO, sob o contexto dele, sem
    # a escolha pontual da origem (SEQ de outro Fire).
    assert check_falso["check"] == [("nasmar", "nasmar", {})]
    assert check_falso["preco"] == [("nasmar", "nasmar")]
    (dest,) = _linha("nasmar")
    assert json.loads(dest["check_json"])["de"] == "nasmar"
    assert json.loads(dest["check_preco_json"]) == {"available": True, "preco_fonte_slug": "mm"}


def test_rota_check_indisponivel_no_destino_nao_grava_check(
    client_ligado, envs_mm_nasmar, monkeypatch
):
    from app.erp import preco_trava, product_check

    monkeypatch.setattr(product_check, "check_order", lambda order, **kw: {"available": False})
    monkeypatch.setattr(preco_trava, "check_preco_para_gravar", lambda *a, **kw: None)
    _semear(envs_mm_nasmar["mm"], check_json='{"de": "mm"}')

    assert _post(client_ligado).status_code == 200
    (dest,) = _linha("nasmar")
    assert dest["check_json"] is None


def test_rota_falha_do_recalculo_nao_desfaz_o_mover(client_ligado, envs_mm_nasmar, monkeypatch):
    from app.erp import product_check
    from app.persistence import roteamento_decisao_repo

    def _explode(order, **kw):  # noqa: ARG001
        raise RuntimeError("Fire caiu")

    monkeypatch.setattr(product_check, "check_order", _explode)
    _semear(envs_mm_nasmar["mm"])

    assert _post(client_ligado).status_code == 200
    assert _linha("mm") == []
    (dest,) = _linha("nasmar")
    assert dest["check_json"] is None
    assert roteamento_decisao_repo.ultima(IMPORT_ID)["acao"] == "movido_apos_importar"


def test_rota_segundo_clique_nao_duplica(client_ligado, envs_mm_nasmar, check_falso):
    from app.persistence import roteamento_decisao_repo

    _semear(envs_mm_nasmar["mm"])
    assert _post(client_ligado).status_code == 200
    r = _post(client_ligado)

    assert r.status_code == 422  # já está na empresa pedida
    assert _linha("mm") == []
    assert len(_linha("nasmar")) == 1
    assert len(roteamento_decisao_repo.listar_por_import(IMPORT_ID)) == 1
    assert len(check_falso["check"]) == 1


def test_rota_conclui_mover_interrompido(client_ligado, envs_mm_nasmar, check_falso, monkeypatch):
    """Pedido nos dois bancos (mover interrompido): a rota acha a origem pela
    empresa que NÃO é o destino e conclui."""
    _semear(envs_mm_nasmar["mm"])
    real = mp._apagar_na_origem
    monkeypatch.setattr(mp, "_apagar_na_origem", lambda conn, import_id: 1 / 0)  # noqa: ARG005
    with pytest.raises(ZeroDivisionError):
        _mover(envs_mm_nasmar)
    monkeypatch.setattr(mp, "_apagar_na_origem", real)

    r = _post(client_ligado)

    assert r.status_code == 200
    assert _linha("mm") == []
    assert len(_linha("nasmar")) == 1
    from app.persistence import roteamento_decisao_repo

    # O interrompido não deixou rastro: o que conclui registra, uma vez.
    (reg,) = roteamento_decisao_repo.listar_por_import(IMPORT_ID)
    assert reg["acao"] == "movido_apos_importar" and reg["env_escolhido"] == "nasmar"


# ── Fix round 1: trava por pedido, inversão, rastro e memória ────────────────


def test_mover_desiste_quando_envio_segura_o_pedido(envs_mm_nasmar, monkeypatch):
    _semear(envs_mm_nasmar["mm"])
    monkeypatch.setattr(mp, "_ESPERA_S", 0.05)
    with mp.trava_do_pedido(IMPORT_ID), pytest.raises(mp.PedidoOcupado):
        _mover(envs_mm_nasmar)
    _nada_mudou(envs_mm_nasmar)
    assert mp._TRAVAS == {}


def test_envio_que_ganhou_fire_codigo_barra_o_mover_que_esperava(envs_mm_nasmar):
    """O envio segura a trava e grava fire_codigo; o mover, que esperava, vê
    o número do Fire na guarda e não move."""
    import threading

    _semear(envs_mm_nasmar["mm"])
    resultado = {}

    def _mover_em_thread():
        try:
            _mover(envs_mm_nasmar)
        except Exception as exc:  # noqa: BLE001
            resultado["exc"] = exc

    with mp.trava_do_pedido(IMPORT_ID):
        t = threading.Thread(target=_mover_em_thread)
        t.start()
        t.join(0.2)
        assert t.is_alive()  # esperando a trava
        with router.env_connect("mm") as conn:
            conn.execute("UPDATE imports SET fire_codigo = 5083 WHERE id = ?", (IMPORT_ID,))
    t.join(5)

    assert isinstance(resultado["exc"], mp.PedidoNaoMovivel)
    assert resultado["exc"].motivo == "já foi para o Fire"
    assert _linha("mm")[0]["fire_codigo"] == 5083
    assert _linha("nasmar") == []


@pytest.mark.parametrize("envio", ["_send_one_to_fire", "_export_one_xlsx"])
def test_envio_espera_o_mover_e_recusa_pedido_que_saiu_da_empresa(
    envs_mm_nasmar, monkeypatch, envio
):
    """O mover segura a trava; o envio espera e, ao pegar a trava, não acha mais
    o pedido nesta empresa: recusa sem ir ao Fire nem gerar planilha."""
    import threading

    from app.exporters import erp_exporter, firebird_exporter
    from app.persistence import context as env_context
    from app.web import server

    def _proibido(*a, **kw):
        raise AssertionError("não pode ir ao Fire/planilha")

    monkeypatch.setattr(firebird_exporter, "FirebirdExporter", _proibido)
    monkeypatch.setattr(erp_exporter, "ERPExporter", _proibido)
    _semear(envs_mm_nasmar["mm"])
    dentro, libera = threading.Event(), threading.Event()
    real = mp._copiar_no_destino

    def _copiar_devagar(*a, **kw):
        dentro.set()
        libera.wait(5)
        return real(*a, **kw)

    monkeypatch.setattr(mp, "_copiar_no_destino", _copiar_devagar)
    mm = envs_mm_nasmar["mm"]
    saida = {}

    def _enviar():
        with env_context.active_env(mm["id"], mm["slug"]):
            saida["r"] = getattr(server, envio)(IMPORT_ID, {"export_mode": "db"}, request_env=mm)

    m = threading.Thread(target=lambda: _mover(envs_mm_nasmar))
    m.start()
    assert dentro.wait(5)
    e = threading.Thread(target=_enviar)
    e.start()
    e.join(0.2)
    assert e.is_alive()  # o envio espera o mover
    libera.set()
    m.join(5)
    e.join(5)

    assert saida["r"].ok is False
    assert saida["r"].reason == "not_found"
    assert _linha("mm") == []
    assert len(_linha("nasmar")) == 1
    assert mp._TRAVAS == {}


def test_rota_409_quando_o_pedido_esta_sendo_enviado(client_ligado, envs_mm_nasmar, monkeypatch):
    _semear(envs_mm_nasmar["mm"])
    monkeypatch.setattr(mp, "_ESPERA_S", 0.05)
    with mp.trava_do_pedido(IMPORT_ID):
        r = _post(client_ligado)
    assert r.status_code == 409
    assert "sendo enviado agora" in r.json()["detail"]
    _nada_mudou(envs_mm_nasmar)


def test_mandar_de_volta_na_janela_do_interrompido_registra_a_volta(envs_mm_nasmar, monkeypatch):
    """mm→nasmar interrompido (pedido nos dois); o operador manda de nasmar para
    mm: o pedido fica na mm e o último "movido" diz nasmar→mm."""
    _semear(envs_mm_nasmar["mm"])
    real = mp._apagar_na_origem
    monkeypatch.setattr(mp, "_apagar_na_origem", lambda conn, import_id: 1 / 0)  # noqa: ARG005
    with pytest.raises(ZeroDivisionError):
        _mover(envs_mm_nasmar)
    monkeypatch.setattr(mp, "_apagar_na_origem", real)

    assert _mover(envs_mm_nasmar, de="nasmar", para="mm") == "movido"

    assert _linha("nasmar") == []
    assert len(_linha("mm")) == 1
    movidos = [
        json.loads(a["detail_json"])
        for a in _linha("mm", "audit_log", "import_id")
        if a["event_type"] == "movido_de_empresa"
    ]
    assert movidos[-1] == {"de": "nasmar", "para": "mm", "por": "ana@mm"}


def test_rota_ultima_decisao_falha_registra_sem_sugestao(
    client_ligado, envs_mm_nasmar, check_falso, monkeypatch
):
    from app.persistence import roteamento_decisao_repo

    _semear(envs_mm_nasmar["mm"])
    real = roteamento_decisao_repo.ultima

    def _explode(import_id):  # noqa: ARG001
        raise RuntimeError("shared travado")

    monkeypatch.setattr(roteamento_decisao_repo, "ultima", _explode)
    assert _post(client_ligado).status_code == 200
    monkeypatch.setattr(roteamento_decisao_repo, "ultima", real)
    ultima = roteamento_decisao_repo.ultima(IMPORT_ID)
    assert ultima["acao"] == "movido_apos_importar"
    assert ultima["env_sugerido"] is None


def test_rota_em_ligado_ensina_a_memoria(client_ligado, envs_mm_nasmar, check_falso):
    from app.persistence import decisao_ambiente_repo

    _semear(envs_mm_nasmar["mm"])
    assert _post(client_ligado).status_code == 200
    lembrada = decisao_ambiente_repo.lembrada("11222333000181")
    assert lembrada["env_slug"] == "nasmar"
    assert lembrada["decidido_por"] == "test@portal.local"


def test_rota_em_observando_nao_ensina_a_memoria(client, envs_mm_nasmar, check_falso):
    from app.persistence import decisao_ambiente_repo

    roteamento_repo.set_modo("observando", por="t")
    _semear(envs_mm_nasmar["mm"])
    assert _post(client).status_code == 200
    assert decisao_ambiente_repo.lembrada("11222333000181") is None


def test_rota_falha_da_memoria_nao_derruba_o_mover(
    client_ligado, envs_mm_nasmar, check_falso, monkeypatch
):
    from app.persistence import decisao_ambiente_repo

    def _explode(**kw):  # noqa: ARG001
        raise RuntimeError("shared travado")

    monkeypatch.setattr(decisao_ambiente_repo, "lembrar", _explode)
    _semear(envs_mm_nasmar["mm"])
    assert _post(client_ligado).status_code == 200
    assert _linha("mm") == []
    assert len(_linha("nasmar")) == 1


# ── Fix round 2: movers cruzados ─────────────────────────────────────────────


def test_dois_movers_cruzados_concluem_sem_database_locked(envs_mm_nasmar, monkeypatch):
    """X mm→nasmar e Y nasmar→mm ao mesmo tempo: cada um trava a própria
    origem e precisaria do banco do outro. O lock global do mover serializa a
    parte de banco: os dois concluem, nenhum "database is locked"."""
    import threading
    import time

    _semear(envs_mm_nasmar["mm"], iid="X")
    _semear(envs_mm_nasmar["nasmar"], iid="Y")
    real = mp._copiar_no_destino

    def _copiar_devagar(*a, **kw):
        time.sleep(0.3)  # segura a origem travada: sem o lock global, cruza
        return real(*a, **kw)

    monkeypatch.setattr(mp, "_copiar_no_destino", _copiar_devagar)
    erros = []
    largada = threading.Barrier(2)

    def _mover_um(iid, de, para):
        largada.wait()
        try:
            mp.mover_import(iid, origem=envs_mm_nasmar[de], destino=envs_mm_nasmar[para], por="t")
        except Exception as exc:  # noqa: BLE001
            erros.append(exc)

    threads = [
        threading.Thread(target=_mover_um, args=("X", "mm", "nasmar")),
        threading.Thread(target=_mover_um, args=("Y", "nasmar", "mm")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)

    assert erros == []
    with router.env_connect("mm") as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM imports")] == ["Y"]
    with router.env_connect("nasmar") as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM imports")] == ["X"]


def test_rota_pedido_ilegivel_no_destino_move_sem_check_nem_memoria(
    client_ligado, envs_mm_nasmar, check_falso, monkeypatch
):
    from app.persistence import decisao_ambiente_repo, repo

    def _explode(import_id):  # noqa: ARG001
        raise RuntimeError("snapshot podre")

    monkeypatch.setattr(repo, "get_import", _explode)
    _semear(envs_mm_nasmar["mm"])
    assert _post(client_ligado).status_code == 200
    assert _linha("mm") == []
    assert _linha("nasmar")[0]["check_json"] is None
    assert check_falso["check"] == []
    assert decisao_ambiente_repo.lembrada("11222333000181") is None


def _detalhe(c):
    r = c.get(f"/api/imported/{IMPORT_ID}/preview")
    assert r.status_code == 200, r.text
    return r.json()


def test_detalhe_expoe_empresa_e_pode_mover(client_ligado, envs_mm_nasmar):
    _semear(envs_mm_nasmar["mm"])
    body = _detalhe(client_ligado)
    assert body["env_slug"] == "mm"
    assert body["pode_mover"] is True
    assert body["motivo_nao_move"] is None
    assert body["roteamento_modo"] == "ligado"


@pytest.mark.parametrize(
    ("cols", "motivo"),
    [
        ({"output_files_json": '["PEDIDO.xlsx"]'}, "já foi exportado em XLSX"),
        ({"fire_codigo": 5083}, "já foi para o Fire"),
    ],
)
def test_detalhe_usa_a_mesma_guarda_do_mover(client_ligado, envs_mm_nasmar, cols, motivo):
    _semear(envs_mm_nasmar["mm"], **cols)
    body = _detalhe(client_ligado)
    assert body["pode_mover"] is False
    assert body["motivo_nao_move"] == motivo


# ── Revisão final: desligado bit-a-bit no detalhe e teto da espera do envio ──


def test_detalhe_em_desligado_nao_calcula_mover(client, envs_mm_nasmar, monkeypatch):
    """C1: em desligado o detalhe não lê a guarda do mover (zero I/O novo) e
    não oferece mover; o modo viaja no payload para a tela não buscar nada."""

    def _proibido(import_id):  # noqa: ARG001
        raise AssertionError("desligado não calcula a guarda do mover")

    monkeypatch.setattr(mp, "motivo_nao_move", _proibido)
    roteamento_repo.set_modo("desligado", por="t")
    _semear(envs_mm_nasmar["mm"])
    client.cookies.set("portal_env", envs_mm_nasmar["mm"]["id"])

    body = _detalhe(client)

    assert body["roteamento_modo"] == "desligado"
    assert body["pode_mover"] is False
    assert body["motivo_nao_move"] is None


def test_detalhe_com_guarda_quebrada_abre_sem_mover(client_ligado, envs_mm_nasmar, monkeypatch):
    """C1: erro na guarda do mover nunca derruba a abertura do pedido."""

    def _explode(import_id):  # noqa: ARG001
        raise RuntimeError("banco travado")

    monkeypatch.setattr(mp, "motivo_nao_move", _explode)
    _semear(envs_mm_nasmar["mm"])

    body = _detalhe(client_ligado)

    assert body["pode_mover"] is False
    assert body["motivo_nao_move"] is None


@pytest.mark.parametrize("envio", ["_send_one_to_fire", "_export_one_xlsx"])
def test_envio_desiste_com_erro_claro_quando_a_trava_nao_sai(envs_mm_nasmar, monkeypatch, envio):
    """R13/M8: a espera do envio pela trava do pedido tem teto: estourou,
    recusa com mensagem clara, sem ir ao Fire nem gerar planilha."""
    from app.persistence import context as env_context
    from app.web import server

    monkeypatch.setattr(mp, "_ESPERA_ENVIO_S", 0.05)
    _semear(envs_mm_nasmar["mm"])
    mm = envs_mm_nasmar["mm"]
    with mp.trava_do_pedido(IMPORT_ID), env_context.active_env(mm["id"], mm["slug"]):
        r = getattr(server, envio)(IMPORT_ID, {"export_mode": "db"}, request_env=mm)

    assert r.ok is False
    assert r.reason == "pedido_ocupado"
    assert r.http_status == 409
    assert "em andamento" in r.detail
    assert _linha("mm")[0]["fire_codigo"] is None
    assert mp._TRAVAS == {}


# ── Revisão final: mover inacabado (I2), duas origens (M2), banco ocupado
#    (M3) e concluir sem repetir rastro (M4) ─────────────────────────────────


def _interromper(envs_mm_nasmar, monkeypatch):
    """mm→nasmar interrompido: o pedido fica nos dois bancos."""
    real = mp._apagar_na_origem
    monkeypatch.setattr(mp, "_apagar_na_origem", lambda conn, import_id: 1 / 0)  # noqa: ARG005
    with pytest.raises(ZeroDivisionError):
        _mover(envs_mm_nasmar)
    monkeypatch.setattr(mp, "_apagar_na_origem", real)
    assert len(_linha("mm")) == 1 and len(_linha("nasmar")) == 1


@pytest.mark.parametrize("modo", ["desligado", "observando", "ligado"])
@pytest.mark.parametrize("envio", ["_send_one_to_fire", "_export_one_xlsx"])
def test_envio_recusa_pedido_com_mover_inacabado(envs_mm_nasmar, monkeypatch, envio, modo):
    """I2: pedido nas duas empresas não vai ao Fire nem vira planilha em
    nenhuma delas, em qualquer modo."""
    from app.exporters import erp_exporter, firebird_exporter
    from app.persistence import context as env_context
    from app.web import server

    def _proibido(*a, **kw):
        raise AssertionError("não pode ir ao Fire/planilha")

    monkeypatch.setattr(firebird_exporter, "FirebirdExporter", _proibido)
    monkeypatch.setattr(erp_exporter, "ERPExporter", _proibido)
    roteamento_repo.set_modo(modo, por="t")
    _semear(envs_mm_nasmar["mm"])
    _interromper(envs_mm_nasmar, monkeypatch)

    for slug in ("mm", "nasmar"):
        env = envs_mm_nasmar[slug]
        with env_context.active_env(env["id"], env["slug"]):
            r = getattr(server, envio)(IMPORT_ID, {"export_mode": "db"}, request_env=env)
        assert r.ok is False
        assert r.reason == "mover_inacabado"
        assert r.http_status == 409
        assert r.detail == "Mover inacabado: conclua o mover do pedido antes de enviar."


def test_rota_conclui_mover_interrompido_em_desligado(client, envs_mm_nasmar, check_falso, monkeypatch):
    """I2: em desligado a rota só conclui (pedido já nos dois bancos)."""
    _semear(envs_mm_nasmar["mm"])
    _interromper(envs_mm_nasmar, monkeypatch)
    roteamento_repo.set_modo("desligado", por="t")

    r = _post(client)

    assert r.status_code == 200
    assert _linha("mm") == []
    assert len(_linha("nasmar")) == 1


def test_rota_desligado_nao_comeca_mover_novo_nem_no_sentido_inverso(
    client, envs_mm_nasmar, monkeypatch
):
    _semear(envs_mm_nasmar["mm"])
    _interromper(envs_mm_nasmar, monkeypatch)
    roteamento_repo.set_modo("desligado", por="t")
    # Destino sem cópia do pedido: é mover novo, recusado em desligado.
    environments_repo.create(
        slug="terceira", name="Terceira", watch_dir="/x/t/in", output_dir="/x/t/out",
        fb_path="/x/t.fdb",
    )
    assert _post(client, "terceira").status_code == 409
    assert len(_linha("mm")) == 1 and len(_linha("nasmar")) == 1


def test_rota_pedido_em_duas_origens_e_409(client_ligado, envs_mm_nasmar, monkeypatch):
    """M2: pedido em mais de uma empresa além do destino: não escolhe uma."""
    terceira = environments_repo.create(
        slug="terceira", name="Terceira", watch_dir="/x/t/in", output_dir="/x/t/out",
        fb_path="/x/t.fdb",
    )
    _semear(envs_mm_nasmar["mm"])
    _semear(terceira)

    r = _post(client_ligado)

    assert r.status_code == 409
    assert r.json()["detail"] == (
        "O pedido está em duas empresas; conclua o mover anterior antes de mover de novo."
    )
    assert len(_linha("mm")) == 1 and _linha("nasmar") == []


def test_rota_banco_ocupado_e_409(client_ligado, envs_mm_nasmar, monkeypatch):
    """M3: "database is locked" no mover vira 409 para tentar de novo, nunca 500."""
    import sqlite3

    def _travado(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(mp, "mover_import", _travado)
    _semear(envs_mm_nasmar["mm"])

    r = _post(client_ligado)

    assert r.status_code == 409
    assert "tente de novo em instantes" in r.json()["detail"]


def test_rota_conclui_sem_segundo_rastro_nem_memoria(
    client_ligado, envs_mm_nasmar, check_falso, monkeypatch
):
    """M4: o rastro já tem este mover: concluir não grava outro nem ensina de novo."""
    from app.persistence import decisao_ambiente_repo, roteamento_decisao_repo

    _semear(envs_mm_nasmar["mm"])
    _interromper(envs_mm_nasmar, monkeypatch)
    roteamento_decisao_repo.registrar(
        import_id=IMPORT_ID, modo="ligado", degrau=None, env_sugerido=None,
        env_escolhido="nasmar", decidido_por="ana@mm", motivo="movido de mm para nasmar",
        acao="movido_apos_importar",
    )

    assert _post(client_ligado).status_code == 200

    assert len(roteamento_decisao_repo.listar_por_import(IMPORT_ID)) == 1
    assert decisao_ambiente_repo.lembrada("11222333000181") is None
    assert _linha("mm") == []
