"""Log de leituras no watcher: uma linha por arquivo lido, rejeitado ou ignorado."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app import pipeline
from app.observability import leituras
from app.persistence import roteamento_repo
from app.worker.jobs import scan_environments
from tests import test_scan_environments_roteamento as base
from tests.test_scan_environments_roteamento import _arquivo, _imports, _order

dois_ambientes = base.dois_ambientes  # fixture reusada

SAMPLE = Path(__file__).parent.parent / "samples" / "Pedido Authentic Feet AF072 QUAN.xlsx"


@pytest.fixture
def capturadas(monkeypatch):
    linhas: list[dict] = []
    monkeypatch.setattr(leituras, "escrever", lambda linha: linhas.append(linha))
    scan_environments._EXTENSOES_REGISTRADAS.clear()
    scan_environments._PENDENCIAS_REGISTRADAS.clear()
    return linhas


def _copiar_sample(tmp_path, slug="mm"):
    dst = tmp_path / slug / "in" / SAMPLE.name
    shutil.copy(SAMPLE, dst)
    return dst


def test_arquivo_valido_gera_linha_importado(dois_ambientes, capturadas):
    roteamento_repo.set_modo("desligado", por="t")
    _copiar_sample(dois_ambientes)
    scan_environments.run_scan()
    (linha,) = capturadas
    imports = _imports("mm")
    assert len(imports) == 1
    assert linha["origem"] == "worker"
    assert linha["ambiente"] == "mm"
    assert linha["resultado"] == "importado"
    assert linha["etapa"] == "parseado"
    assert linha["trace_id"] == imports[0]["trace_id"]


def test_duplicado_gera_linha_skip_duplicate(dois_ambientes, capturadas):
    roteamento_repo.set_modo("desligado", por="t")
    _copiar_sample(dois_ambientes)
    scan_environments.run_scan()
    capturadas.clear()
    _copiar_sample(dois_ambientes)  # mesmo conteudo, mesmo sha
    scan_environments.run_scan()
    (linha,) = capturadas
    assert linha["origem"] == "worker"
    assert linha["etapa"] == "rejeitado"
    assert linha["rejeicao"]["motivo"] == "skip_duplicate"
    assert linha["resultado"] == "skip_duplicate"
    assert linha["arquivo"]["nome"] == SAMPLE.name


def test_extensao_nao_suportada_gera_uma_linha_em_dois_ciclos(dois_ambientes, capturadas):
    (dois_ambientes / "mm" / "in" / "anotacao.txt").write_text("oi")
    scan_environments.run_scan()
    scan_environments.run_scan()
    (linha,) = capturadas
    assert linha["etapa"] == "rejeitado"
    assert linha["rejeicao"]["motivo"] == "extensao"
    assert linha["arquivo"]["nome"] == "anotacao.txt"
    assert linha["usuario"] is None


def test_excecao_no_parse_gera_linha_erro_e_nao_propaga(dois_ambientes, capturadas, monkeypatch):
    roteamento_repo.set_modo("desligado", por="t")

    def quebra(*a, **k):
        raise RuntimeError("parser explodiu")

    monkeypatch.setattr(pipeline, "process_detailed", quebra)
    p = _copiar_sample(dois_ambientes)
    scan_environments.run_scan()  # nao levanta
    (linha,) = capturadas
    assert linha["etapa"] == "erro"
    assert linha["resultado"] == "erro"
    assert not p.exists()  # seguiu o caminho de erro de sempre (com_erro)


def test_pendencia_throttle_gera_linha(dois_ambientes, capturadas, monkeypatch):
    roteamento_repo.set_modo("ligado", por="t")
    monkeypatch.setattr(pipeline, "process_detailed", lambda f, d=None: _order(None))
    _arquivo(dois_ambientes)
    scan_environments.run_scan()  # retem: 1 linha de parse (retido)
    assert capturadas[-1]["resultado"] == "retido"
    capturadas.clear()
    scan_environments.run_scan()  # throttle
    (linha,) = capturadas
    assert linha["rejeicao"]["motivo"] == "pendencia_throttle"
    assert linha["resultado"] == "pendencia_throttle"
    capturadas.clear()
    scan_environments.run_scan()  # 3o ciclo: mesmo sha, nenhuma linha nova
    assert capturadas == []


def test_falha_de_insert_no_worker_grava_excecao(dois_ambientes, capturadas, monkeypatch):
    roteamento_repo.set_modo("desligado", por="t")

    def quebra(entry):
        raise OSError("banco travado")

    monkeypatch.setattr(scan_environments.repo, "insert_import", quebra)
    _copiar_sample(dois_ambientes)
    scan_environments.run_scan()
    (linha,) = capturadas
    assert linha["resultado"] == "erro"
    assert linha["excecao"]["tipo"] == "OSError" and linha["excecao"]["msg"] == "banco travado"
