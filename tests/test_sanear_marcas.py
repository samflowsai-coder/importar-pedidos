"""tools/sanear_marcas_fire.py e app/erp/backup.py — só dublês, nenhum Fire."""
from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.erp import backup as backup_mod

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "sanear_marcas_fire.py"
_spec = importlib.util.spec_from_file_location("sanear_marcas_fire", _TOOL)
tool = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tool  # @dataclass resolve tipos via sys.modules
_spec.loader.exec_module(tool)

chave_solta, normalizar, planejar = tool.chave_solta, tool.normalizar, tool.planejar


def test_normalizar_igual_ao_flow():
    assert normalizar("  Sam's   Club ") == "SAM'S CLUB"
    assert normalizar("Têxtil") == "TÊXTIL"


def test_plano_da_mm_em_17_10():
    existentes = [(1, ""), (2, "Adatex S. A. Industrial e Come"), (16, "Zanotti S.A.")]
    plano = planejar(existentes, ["Kolosh", "Sam's Club"])
    assert plano.ja_existem == [] and plano.parecidas == []
    assert plano.inserir == [(17, "Kolosh"), (18, "Sam's Club")]


def test_avisa_grafia_parecida():
    plano = planejar([(5, "SAMS CLUB")], ["Sam's Club"])
    assert plano.parecidas == [("Sam's Club", "SAMS CLUB", 5)] and plano.inserir == []


def test_reconhece_existente_pela_regra_do_flow():
    plano = planejar([(5, "kolosh ")], ["Kolosh"])
    assert plano.ja_existem == [("Kolosh", 5)] and plano.inserir == []


def test_chave_solta_tira_acento_e_apostrofo():
    assert chave_solta("Sam's Club") == chave_solta("SAMS CLUB")


def test_17_marcas_da_mm_vao_de_17_a_33():
    existentes = [(1, ""), *[(i, f"Forn {i}") for i in range(2, 17)]]
    plano = planejar(existentes, tool.MARCAS_FLOW_2026_10_17)
    assert len(tool.MARCAS_FLOW_2026_10_17) == 17
    assert [c for c, _ in plano.inserir] == list(range(17, 34))
    assert "Sam's Club" in [n for _, n in plano.inserir]


# --- executar(): dublês de conexão e backup ---------------------------------


class _Cur:
    def __init__(self, conn):
        self.c = conn

    def execute(self, sql, params=()):
        self.c.log.append(sql.split()[0])
        if sql.startswith("INSERT"):
            if self.c.falha_insert:
                raise RuntimeError("boom")
            self.c.pend.append(params)
        elif sql.startswith("SELECT"):
            self.c.visivel = self.c.linhas + ([] if self.c.esconde else self.c.pend)

    def fetchall(self):
        return list(self.c.visivel)


class _Conn:
    def __init__(self, linhas, esconde=False, falha_insert=False):
        self.linhas, self.pend, self.visivel = list(linhas), [], []
        self.esconde, self.falha_insert = esconde, falha_insert
        self.log, self.commits, self.rollbacks = [], 0, 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1
        self.linhas += self.pend
        self.pend = []

    def rollback(self):
        self.rollbacks += 1
        self.pend = []


class _Fabrica:
    def __init__(self, linhas, **kw):
        self.leitura = _Conn(linhas)
        self.escrita = _Conn(linhas, **kw)
        self.usadas = []

    @contextmanager
    def connect_somente_leitura(self, cfg):
        self.usadas.append("leitura")
        yield self.leitura

    @contextmanager
    def connect_with_config(self, cfg):
        self.usadas.append("escrita")
        yield self.escrita


BASE = [(1, ""), (2, "Forn")]


def _rodar(fab, gravar, backup=None, desejadas=("Kolosh", "Sam's Club")):
    saida = []
    cod = tool.executar(
        {"host": "h"}, gravar=gravar, backup_servidor="C:\\b.fbk",
        desejadas=list(desejadas), conn_factory=fab, backup=backup, out=saida.append,
    )
    return cod, saida


def test_dry_run_so_le_e_nao_faz_backup():
    fab = _Fabrica(BASE)
    chamou = []
    cod, saida = _rodar(fab, False, backup=lambda *a: chamou.append(a))
    assert cod == 0 and fab.usadas == ["leitura"] and chamou == []
    assert saida[-1] == "Dry-run: nada gravado."


def test_gravar_faz_backup_antes_insere_confere_e_commita():
    fab = _Fabrica(BASE)
    ordem = []
    cod, _ = _rodar(fab, True, backup=lambda cfg, dest: ordem.append(("backup", dest)))
    assert cod == 0 and ordem == [("backup", "C:\\b.fbk")]
    assert fab.escrita.commits == 1 and fab.escrita.rollbacks == 0
    assert (3, "Kolosh") in fab.escrita.linhas and (4, "Sam's Club") in fab.escrita.linhas


def test_gravar_para_se_ha_parecida():
    fab = _Fabrica([(5, "SAMS CLUB")])
    chamou = []
    cod, _ = _rodar(fab, True, backup=lambda *a: chamou.append(a))
    assert cod == 1 and chamou == [] and fab.usadas == ["leitura"]


def test_gravar_sem_backup_nao_grava():
    fab = _Fabrica(BASE)

    def falha(cfg, dest):
        raise RuntimeError("sem disco")

    with pytest.raises(RuntimeError):
        _rodar(fab, True, backup=falha)
    assert "escrita" not in fab.usadas


def test_divergencia_na_conferencia_faz_rollback_e_sai_1():
    fab = _Fabrica(BASE, esconde=True)
    cod, _ = _rodar(fab, True, backup=lambda *a: None)
    assert cod == 1 and fab.escrita.rollbacks >= 1 and fab.escrita.commits == 0


def test_erro_no_insert_faz_rollback_e_sai_1():
    fab = _Fabrica(BASE, falha_insert=True)
    cod, _ = _rodar(fab, True, backup=lambda *a: None)
    assert cod == 1 and fab.escrita.rollbacks == 1 and fab.escrita.commits == 0


def test_nada_a_inserir_nao_faz_backup():
    fab = _Fabrica([(3, "Kolosh")])
    chamou = []
    cod, _ = _rodar(fab, True, backup=lambda *a: chamou.append(a), desejadas=["Kolosh"])
    assert cod == 0 and chamou == []


# --- backup.gbak -------------------------------------------------------------


class _Svc:
    def __init__(self):
        self.calls = []

    def backup(self, origem, destino):
        self.calls.append(("backup", origem, destino))

    def wait(self):
        self.calls.append(("wait",))

    def close(self):
        self.calls.append(("close",))


class _Services:
    def __init__(self):
        self.svc, self.kw = _Svc(), None

    def connect(self, **kw):
        self.kw = kw
        return self.svc


def test_gbak_conecta_faz_backup_espera_e_fecha():
    s = _Services()
    cfg = {"host": "srv", "path": "C:\\Fire\\mm.fdb", "user": "SYSDBA", "password": "x"}
    backup_mod.gbak(cfg, "C:\\bk\\a.fbk", services=s)
    assert s.kw == {"host": "srv", "user": "SYSDBA", "password": "x"}
    assert s.svc.calls == [("backup", "C:\\Fire\\mm.fdb", "C:\\bk\\a.fbk"), ("wait",), ("close",)]


def test_gbak_recusa_embedded():
    with pytest.raises(ValueError):
        backup_mod.gbak({"host": "", "path": "x.fdb"}, "a.fbk", services=_Services())
