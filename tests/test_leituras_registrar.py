import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from loguru import logger

from app.ingestion.file_loader import LoadedFile
from app.models.order import Order, OrderHeader, OrderItem
from app.observability import leituras
from app.persistence import db, repo

SAMPLES = Path(__file__).parent.parent / "samples"

CHAVES = {
    "v", "ts", "portal", "trace_id", "origem", "ambiente", "usuario", "arquivo", "etapa",
    "rejeicao", "extracao", "parser", "pedido", "validacao", "suspeito", "fire_check",
    "resultado", "excecao", "ms_total",
}  # fmt: skip


@pytest.fixture
def capturadas(monkeypatch):
    linhas: list[dict] = []
    monkeypatch.setattr(leituras, "escrever", lambda linha: linhas.append(linha))
    return linhas


def _order():
    return Order(
        header=OrderHeader(order_number="AF072", customer_name="Loja X", customer_cnpj="1122"),
        items=[
            OrderItem(description="Sandalia Aurora", product_code="A", quantity=2, unit_price=10),
            OrderItem(description="Sandalia Lua", product_code="B", quantity=3, unit_price=10),
        ],
    )


def _loaded(raw=b"abc"):
    return LoadedFile(path=Path("pedido.xlsx"), extension=".xlsx", raw=raw)


def test_linha_de_sucesso_tem_todas_as_chaves(capturadas, monkeypatch):
    from app import pipeline

    monkeypatch.setattr(pipeline, "process_detailed", lambda f, d=None: _order())
    raw = b"conteudo"
    with leituras.registrar(
        origem="upload", nome="pedido.xlsx", raw=raw, usuario="a@b.c", ambiente="mm",
        copia="/x/recebidos/p.xlsx",
    ) as leit:  # fmt: skip
        order = leit.processar(_loaded(raw))
        leit.check = {"available": False, "reason": "FB_DATABASE_NOT_SET", "items": []}
        leit.resultado = "preview"
    assert order is leit.order and order is not None
    (linha,) = capturadas
    assert set(linha) == CHAVES
    assert linha["v"] == 1 and linha["origem"] == "upload" and linha["etapa"] == "parseado"
    assert linha["ambiente"] == "mm" and linha["usuario"] == "a@b.c"
    assert linha["arquivo"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert linha["arquivo"]["tamanho"] == len(raw) and linha["arquivo"]["ext"] == ".xlsx"
    assert linha["arquivo"]["copia"] == "/x/recebidos/p.xlsx"
    assert linha["pedido"]["numero"] == "AF072" and linha["pedido"]["itens"] == 2
    assert linha["fire_check"]["reason"] == "FB_DATABASE_NOT_SET"
    assert linha["resultado"] == "preview" and linha["excecao"] is None
    assert linha["rejeicao"] is None and isinstance(linha["ms_total"], int)
    assert set(linha["portal"]) == {"versao", "git_commit"}
    assert linha["suspeito"]["sinais"] == []
    assert "env" not in json.dumps(linha)


def test_http_exception_vira_resultado_e_relevanta(capturadas, monkeypatch):
    from app import pipeline

    monkeypatch.setattr(pipeline, "process_detailed", lambda f, d=None: None)
    with pytest.raises(HTTPException) as ei:
        with leituras.registrar(origem="upload", nome="p.xlsx") as leit:
            leit.processar(_loaded())
            raise HTTPException(422, "sem itens")
    assert ei.value.status_code == 422
    (linha,) = capturadas
    assert linha["resultado"] == "422" and linha["excecao"] is None
    assert set(linha) == CHAVES


def test_excecao_generica_etapa_erro_e_relevanta(capturadas):
    with pytest.raises(ValueError):
        with leituras.registrar(origem="lote", nome="p.xlsx"):
            raise ValueError("boom")
    (linha,) = capturadas
    assert linha["etapa"] == "erro" and linha["resultado"] == "erro"
    assert linha["excecao"]["tipo"] == "ValueError" and linha["excecao"]["msg"] == "boom"
    assert linha["excecao"]["onde"] is None  # nenhum frame dentro de app/


def test_excecao_dentro_de_app_aponta_onde(capturadas, monkeypatch):
    from app import pipeline

    def quebra(f, d=None):
        d.fmt = "xlsx"
        raise RuntimeError("no parser")

    monkeypatch.setattr(pipeline, "process_detailed", quebra)
    with pytest.raises(RuntimeError):
        with leituras.registrar(origem="lote", nome="p.xlsx") as leit:
            leit.processar(_loaded())
    (linha,) = capturadas
    # ultimo frame dentro de app/: o `.processar` do proprio registrador
    assert linha["excecao"]["onde"].startswith("leituras.py:")
    assert linha["extracao"] is None or linha["extracao"]["fmt"] == "xlsx"


def test_sem_ctx_com_trace_id(capturadas):
    from app.observability.trace import with_trace_id

    with with_trace_id("abc123"):
        with leituras.registrar(origem="lote", nome="p.xlsx"):
            pass
    assert capturadas[0]["trace_id"] == "abc123"
    assert capturadas[0]["etapa"] == "erro"  # nada processado: sem pedido


def test_rejeitado(capturadas):
    leituras.rejeitado(
        "upload", "x.txt", "extensao", detalhe="so xlsx", tamanho=10, usuario="u", ambiente="mm"
    )
    (linha,) = capturadas
    assert set(linha) == CHAVES
    assert linha["etapa"] == "rejeitado"
    assert linha["rejeicao"] == {"motivo": "extensao", "detalhe": "so xlsx"}
    assert linha["arquivo"]["nome"] == "x.txt" and linha["arquivo"]["ext"] == ".txt"
    assert linha["arquivo"]["tamanho"] == 10 and linha["arquivo"]["sha256"] is None
    assert linha["pedido"] is None and linha["resultado"] is None


def test_escrever_nao_levanta_com_falha(monkeypatch):
    def quebra(*a, **k):
        raise OSError("disco cheio")

    monkeypatch.setattr(leituras, "serializar", quebra)
    leituras.escrever({"v": 1})  # nao levanta


def test_escrever_grava_json_valido_uma_por_linha_e_canal_isolado(tmp_path):
    from app.utils.logger import _fora_do_canal, _so_canal

    alvo = tmp_path / "leituras.jsonl"
    outros: list[str] = []
    h1 = logger.add(
        alvo, format="{message}", encoding="utf-8", level="INFO", filter=_so_canal, enqueue=False
    )
    h2 = logger.add(lambda m: outros.append(str(m)), level="DEBUG", filter=_fora_do_canal)
    try:
        leituras.escrever({"v": 1, "cliente": "Calçados Ação", "n": 1})
        leituras.escrever({"v": 1, "cliente": "Outra", "n": 2})
        logger.info("mensagem comum")
    finally:
        logger.remove(h1)
        logger.remove(h2)
    linhas = alvo.read_text(encoding="utf-8").splitlines()
    assert len(linhas) == 2
    assert json.loads(linhas[0])["cliente"] == "Calçados Ação"
    assert "Calçados Ação" in linhas[0]
    assert json.loads(linhas[1])["n"] == 2
    assert len(outros) == 1 and "mensagem comum" in outros[0]  # canal fora dos outros sinks


def test_sinks_existentes_rejeitam_canal():
    from app.utils.logger import _fora_do_canal, _inject_trace_id, _so_canal

    rec_l = {"extra": {"canal": "leituras"}}
    rec_n = {"extra": {}}
    assert _so_canal(rec_l) and not _so_canal(rec_n)
    assert not _fora_do_canal(rec_l) and _fora_do_canal(rec_n)
    assert _inject_trace_id(rec_n) is True


def test_sink_real_do_canal_escreve_no_arquivo_e_nao_chega_ao_pipeline_log(tmp_path):
    from app.utils import logger as lg

    assert lg.LOG_DIR.is_absolute() and lg.LOG_DIR == Path(lg.__file__).resolve().parents[2] / "logs"
    id_sink = lg._configurar_sink_leituras(tmp_path / "logs")  # a mesma funcao do import
    assert id_sink is not None
    gerais: list[str] = []
    id_geral = logger.add(lambda m: gerais.append(str(m)), level="DEBUG", filter=lg._filtro_geral)
    try:
        leituras.escrever({"v": 1, "n": 7})
        logger.info("mensagem comum")
    finally:
        logger.remove(id_geral)
        logger.remove(id_sink)  # drena a fila (enqueue=True)
    linhas = (tmp_path / "logs" / "leituras.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["n"] for x in linhas] == [7]
    assert len(gerais) == 1 and "mensagem comum" in gerais[0]  # canal nao chega ao geral


def test_sink_leituras_sem_permissao_nao_levanta(tmp_path, monkeypatch):
    from app.utils import logger as lg

    def nega(self, *a, **k):
        raise PermissionError("sem permissao")

    monkeypatch.setattr(Path, "mkdir", nega)
    assert lg._configurar_sink_leituras(tmp_path / "x") is None


def test_import_do_logger_sobrevive_a_logs_sem_permissao(tmp_path, monkeypatch):
    import importlib

    from app.utils import logger as lg

    real_mkdir = Path.mkdir

    def nega(self, *a, **k):
        if self.name == "logs" and self.is_absolute():
            raise PermissionError("sem permissao")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", nega)
    try:
        importlib.reload(lg)  # nao levanta
        assert lg._ID_SINK_LEITURAS is None
    finally:
        monkeypatch.undo()
        importlib.reload(lg)
        if lg._ID_SINK_LEITURAS is not None:
            lg.logger.remove(lg._ID_SINK_LEITURAS)
            lg._ID_SINK_LEITURAS = None


def test_versao_portal_le_applied_update(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    (tmp_path / "applied_update.json").write_text(
        json.dumps({"version": "20261005-1136", "git_commit": "8ee8059"}), encoding="utf-8"
    )
    monkeypatch.setattr(leituras, "_VERSAO", None)
    assert leituras.versao_portal() == {"versao": "20261005-1136", "git_commit": "8ee8059"}


def test_versao_portal_ilegivel_desconhecida(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    (tmp_path / "applied_update.json").write_text("{nao json", encoding="utf-8")
    monkeypatch.setattr(leituras, "_VERSAO", None)
    assert leituras.versao_portal() == {"versao": "desconhecida", "git_commit": None}


# --- consultas de duplicidade ------------------------------------------------


@pytest.fixture
def banco(tmp_path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _import(id_, sha, numero="AF072", cnpj="1122", dias=1):
    repo.insert_import(
        {
            "id": id_,
            "source_filename": "p.xlsx",
            "imported_at": (datetime.now() - timedelta(days=dias)).isoformat(),
            "order_number": numero,
            "customer_cnpj": cnpj,
            "file_sha256": sha,
            "snapshot": {},
        }
    )


def test_sha_ja_visto(banco):
    _import("i1", "sha1")
    r = leituras.sha_ja_visto("sha1")
    assert r["import_id"] == "i1" and r["quando"]
    assert leituras.sha_ja_visto("outro") is None
    assert leituras.sha_ja_visto(None) is None


def test_pedido_ja_importado_exige_sha_diferente_e_90_dias(banco):
    _import("i1", "sha1")
    _import("velho", "sha9", numero="OLD", dias=200)
    r = leituras.pedido_ja_importado("AF072", "1122", "sha2")
    assert r["import_id"] == "i1" and r["sha256"] == "sha1" and r["quando"]
    assert leituras.pedido_ja_importado("AF072", "1122", "sha1") is None
    assert leituras.pedido_ja_importado("OLD", "1122", "sha2") is None
    assert leituras.pedido_ja_importado(None, "1122", "sha2") is None


def test_consultas_sem_contexto_de_ambiente_devolvem_none():
    db.set_db_path(None)
    assert leituras.sha_ja_visto("x") is None
    assert leituras.pedido_ja_importado("A", "1", "x") is None


def test_registrar_preenche_duplicidade(capturadas, banco, monkeypatch):
    from app import pipeline

    raw = b"conteudo"
    _import("i1", hashlib.sha256(raw).hexdigest())
    _import("i0", "outrosha")
    monkeypatch.setattr(pipeline, "process_detailed", lambda f, d=None: _order())
    with leituras.registrar(origem="upload", nome="p.xlsx", raw=raw) as leit:
        leit.processar(_loaded(raw))
    (linha,) = capturadas
    assert linha["arquivo"]["sha_ja_visto"]["import_id"] == "i1"
    assert linha["pedido"]["pedido_ja_importado"]["import_id"] in {"i1", "i0"}


def test_sample_real_ponta_a_ponta(capturadas):
    p = SAMPLES / "Pedido Authentic Feet AF072 QUAN.xlsx"
    if not p.exists():
        pytest.skip("sample ausente")
    raw = p.read_bytes()
    with leituras.registrar(origem="upload", nome=p.name, raw=raw) as leit:
        leit.processar(LoadedFile(path=p, extension=".xlsx", raw=raw))
        leit.resultado = "preview"
    (linha,) = capturadas
    assert linha["parser"]["usado"] == "NasmarTemplateParser"
    assert linha["extracao"]["abas"] and "nome" in linha["extracao"]["abas"][0]
    assert "QUAN." in linha["extracao"]["cabecalho_candidato"]["celulas"]
    assert linha["suspeito"]["sinais"] == []
    json.dumps(linha, default=str)


def test_rejeitado_com_resultado(capturadas):
    leituras.rejeitado("worker", "x.xlsx", "skip_duplicate", resultado="skip_duplicate")
    (linha,) = capturadas
    assert linha["resultado"] == "skip_duplicate"


def test_excecao_engolida_por_quem_chamou_ainda_vai_pra_linha(capturadas, monkeypatch):
    """O worker pega a exceção do parse num try/except dentro do `with`: o `__exit__`
    nunca a vê. A linha tem que sair com `excecao` mesmo assim."""
    from app import pipeline

    def quebra(f, d=None):
        d.fmt = "xlsx"
        raise RuntimeError("extractor caiu")

    monkeypatch.setattr(pipeline, "process_detailed", quebra)
    with leituras.registrar(origem="worker", nome="p.xlsx") as leitura:
        try:
            leitura.processar(LoadedFile(path=Path("p.xlsx"), extension=".xlsx", raw=b"x"))
        except Exception:  # noqa: BLE001 — o chamador engole, como o scan_environments
            pass
    (linha,) = capturadas
    assert linha["etapa"] == "erro" and linha["resultado"] == "erro"
    assert linha["excecao"]["tipo"] == "RuntimeError"
    assert linha["excecao"]["msg"] == "extractor caiu"
    assert linha["extracao"]["fmt"] == "xlsx"  # o que já tinha sido coletado não se perde


def test_sha_ja_visto_calculado_na_entrada_nao_enxerga_o_proprio_insert(capturadas, banco, monkeypatch):
    """No worker o `with` engloba o insert do import: a 1a leitura NAO e duplicata."""
    from app import pipeline

    monkeypatch.setattr(pipeline, "process_detailed", lambda f, d=None: _order())
    raw = b"conteudo-do-worker"
    sha = hashlib.sha256(raw).hexdigest()
    for _ in range(2):
        with leituras.registrar(origem="worker", nome="p.xlsx", raw=raw) as leit:
            leit.processar(_loaded(raw))
            if not capturadas:
                _import("i1", sha)  # insert DENTRO do with
    primeira, segunda = capturadas
    assert primeira["arquivo"]["sha_ja_visto"] is None
    assert segunda["arquivo"]["sha_ja_visto"]["import_id"] == "i1"


def test_falha_publica_registra_excecao_engolida_pelo_chamador(capturadas):
    with leituras.registrar(origem="worker", nome="p.xlsx", raw=b"x") as leit:
        try:
            raise OSError("disco cheio no insert")
        except OSError as e:
            leit.falha(e)
        leit.resultado = "erro"
    (linha,) = capturadas
    assert linha["excecao"]["tipo"] == "OSError" and linha["excecao"]["msg"] == "disco cheio no insert"


def test_excecao_pydantic_nao_vaza_input_da_planilha(capturadas):
    with pytest.raises(Exception):  # noqa: B017, PT011
        with leituras.registrar(origem="lote", nome="p.xlsx"):
            OrderItem(quantity="TEXTO-SECRETO-DA-PLANILHA")
    (linha,) = capturadas
    msg = linha["excecao"]["msg"]
    assert "TEXTO-SECRETO" not in msg and "quantity" in msg


def test_quantidade_nan_nao_derruba_a_linha(capturadas, monkeypatch):
    from app import pipeline

    o = _order()
    o.items[0].quantity = float("nan")
    monkeypatch.setattr(pipeline, "process_detailed", lambda f, d=None: o)
    with leituras.registrar(origem="upload", nome="p.xlsx", raw=b"x") as leit:
        leit.processar(_loaded(b"x"))
    (linha,) = capturadas
    assert linha["pedido"]["itens"] == 2 and linha["pedido"]["qtd_total"] == 3


def test_versao_portal_invalida_pela_mtime(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(leituras, "_VERSAO", None)
    assert leituras.versao_portal()["versao"] == "desconhecida"  # arquivo ainda nao existe
    arq = tmp_path / "applied_update.json"
    arq.write_text(json.dumps({"version": "v1", "git_commit": "a"}), encoding="utf-8")
    assert leituras.versao_portal()["versao"] == "v1"
    arq.write_text(json.dumps({"version": "v2", "git_commit": "b"}), encoding="utf-8")
    os.utime(arq, ns=(1, 2_000_000_000))
    assert leituras.versao_portal() == {"versao": "v2", "git_commit": "b"}


def test_onde_ignora_caixa_do_caminho(monkeypatch):
    import types

    monkeypatch.setattr(leituras.os.path, "normcase", lambda p: p.lower())
    raiz = str(Path(leituras.__file__).resolve().parents[1])
    code = types.SimpleNamespace(co_filename=raiz.upper() + "/x.py")
    tb = types.SimpleNamespace(tb_frame=types.SimpleNamespace(f_code=code), tb_lineno=9, tb_next=None)
    assert leituras._onde(tb) == "x.py:9"


def test_cliente_igual_ao_ambiente_so_do_ambiente_da_linha(capturadas, monkeypatch):
    from app import pipeline
    from app.persistence import environments_repo

    monkeypatch.setattr(
        environments_repo,
        "list_active",
        lambda: [{"slug": "mm", "cnpj": "99999999000199"}, {"slug": "na", "cnpj": "1122"}],
    )
    monkeypatch.setattr(pipeline, "process_detailed", lambda f, d=None: _order())  # cliente 1122
    for amb in ("mm", "na", None):
        with leituras.registrar(origem="worker", nome="p.xlsx", raw=b"x", ambiente=amb) as leit:
            leit.processar(_loaded(b"x"))
    sinais = [c["suspeito"]["sinais"] for c in capturadas]
    forte = [c["suspeito"]["forte"] for c in capturadas]
    assert "cliente_igual_ao_ambiente" not in sinais[0] and forte[0] is False  # outro ambiente
    assert "cliente_igual_ao_ambiente" in sinais[1] and forte[1] is True
    assert "cliente_igual_ao_ambiente" not in sinais[2]  # sem ambiente, sem sinal


def test_rejeitado_tem_suspeito_neutro(capturadas):
    leituras.rejeitado("upload", "x.txt", "extensao")
    assert capturadas[0]["suspeito"] == {"sinais": [], "forte": False}


@pytest.mark.parametrize(
    ("nome", "esperado"),
    [("PEDIDO TENNIS STATION.xlsx", None), ("Pedido Authentic Feet AF072 QUAN.xlsx", "QUAN.")],
)
def test_cabecalho_candidato_samples(nome, esperado):
    from app import pipeline
    from app.observability.leituras import Diagnostico

    p = SAMPLES / nome
    diag = Diagnostico()
    pipeline.process_detailed(LoadedFile(path=p, extension=".xlsx", raw=p.read_bytes()), diag)
    celulas = diag.extracao["cabecalho_candidato"]["celulas"]
    print(nome, diag.extracao["cabecalho_candidato"])
    assert all(len(c) >= 3 and "@" not in c for c in celulas)
    if esperado:
        assert esperado in celulas
