"""Guarda imutável de todo arquivo recebido pelo portal, antes do parse.

O caso que motivou: AF127/AF017 (H2S4, 27/07/2026) — dois arquivos com o mesmo
nome chegaram no mesmo dia, o segundo já vinha com os códigos errados, e não
existia cópia nossa do segundo pra provar de onde veio.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from app.ingestion.arquivo_recebido import guardar, raiz_recebidos

AGORA = datetime(2026, 9, 3, 14, 32, 12)


def test_guarda_bytes_intactos_com_sha256_e_caminho_por_ambiente_e_mes(tmp_path):
    raw = b"conteudo do pedido"

    rec = guardar(raw, "AF127.xlsx", raiz=tmp_path, ambiente="nasmar", agora=AGORA)

    assert rec.path.read_bytes() == raw
    assert rec.sha256 == hashlib.sha256(raw).hexdigest()
    assert rec.path.parent == tmp_path / "nasmar" / "2026" / "09"
    assert rec.path.name == f"20260903-143212_{rec.sha256[:12]}_AF127.xlsx"


def test_nome_original_vira_nome_seguro_sem_caminho_nem_caracteres_proibidos(tmp_path):
    rec = guardar(
        b"x",
        r"..\..\C:\temp\pedido çã o?.xlsx",
        raiz=tmp_path,
        ambiente="mm",
        agora=AGORA,
    )

    nome = rec.path.name
    assert nome.endswith(".xlsx")
    assert nome.endswith("_pedido çã o_.xlsx")  # acento fica, '?' vira '_'
    for proibido in ("/", "\\", "..", "?", ":"):
        assert proibido not in nome.split("_", 2)[2]
    assert rec.path.parent == tmp_path / "mm" / "2026" / "09"


def test_mesmo_conteudo_no_mesmo_segundo_gera_duas_copias_sem_sobrescrever(tmp_path):
    a = guardar(b"igual", "AF127.xlsx", raiz=tmp_path, ambiente="nasmar", agora=AGORA)
    b = guardar(b"igual", "AF127.xlsx", raiz=tmp_path, ambiente="nasmar", agora=AGORA)

    assert a.path != b.path
    assert a.path.exists() and b.path.exists()
    assert a.sha256 == b.sha256
    assert sorted(p.name for p in a.path.parent.iterdir()) == sorted([a.path.name, b.path.name])


def test_sem_ambiente_ativo_cai_na_pasta_sem_ambiente(tmp_path):
    rec = guardar(b"x", "p.pdf", raiz=tmp_path, ambiente=None, agora=AGORA)

    assert rec.path.parent == tmp_path / "_sem-ambiente" / "2026" / "09"


def test_falha_de_escrita_propaga_em_vez_de_engolir(tmp_path):
    raiz_que_e_arquivo = tmp_path / "recebidos"
    raiz_que_e_arquivo.write_text("nao sou pasta")

    with pytest.raises(OSError):
        guardar(b"x", "p.pdf", raiz=raiz_que_e_arquivo, ambiente="nasmar", agora=AGORA)


def test_raiz_recebidos_mora_dentro_do_app_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))

    assert raiz_recebidos() == Path(tmp_path) / "recebidos"
