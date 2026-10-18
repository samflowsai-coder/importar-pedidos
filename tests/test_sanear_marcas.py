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
        self.c.sqls.append(sql.split()[0])
        if sql.startswith("INSERT"):
            if self.c.falha_insert:
                raise self.c.falha_insert
            self.c.pend.append(tuple(params))
        elif "RDB$TRIGGERS" in sql:
            self.c.visivel = [t if isinstance(t, tuple) else (t, 0) for t in self.c.triggers]
        elif "RDB$GENERATORS" in sql:
            self.c.visivel = [(g,) for g in self.c.geradores]
        elif sql.startswith("SELECT"):
            self.c.visivel = self.c.linhas + ([] if self.c.esconde else self.c.pend)
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return list(self.c.visivel)

    def close(self):
        pass


class _Conn:
    def __init__(self, linhas, esconde=False, falha_insert=None, triggers=(), geradores=(),
                 falha_commit=False, falha_rollback=False):
        self.linhas, self.pend, self.visivel = list(linhas), [], []
        self.esconde, self.falha_insert = esconde, falha_insert
        self.triggers, self.geradores, self.falha_commit = triggers, geradores, falha_commit
        self.falha_rollback = falha_rollback
        self.sqls, self.commits, self.rollbacks = [], 0, 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        if self.falha_commit:
            raise RuntimeError("commit caiu")
        self.commits += 1
        self.linhas += self.pend
        self.pend = []

    def commit_na_saida(self):
        """O commit que o `_connect_with` real faz na saída limpa do with."""
        self.linhas += self.pend
        self.pend = []

    def rollback(self):
        self.rollbacks += 1
        if self.falha_rollback:
            raise RuntimeError("rollback caiu")
        self.pend = []


class _Fabrica:
    def __init__(self, linhas, linhas_escrita=None, **kw):
        self.leitura = _Conn(linhas, triggers=kw.pop("triggers", ()), geradores=kw.pop("geradores", ()))
        self.escrita = _Conn(linhas if linhas_escrita is None else linhas_escrita, **kw)
        self.usadas = []

    @contextmanager
    def connect_somente_leitura(self, cfg):
        self.usadas.append("leitura")
        yield self.leitura

    @contextmanager
    def connect_escrita_cadastro(self, cfg):
        self.usadas.append("escrita")
        yield self.escrita
        self.escrita.commit_na_saida()  # como o real: só em saída limpa


BASE = [(1, ""), (2, "Forn")]
CFG = {"host": "h", "port": "3050", "path": "C:\\mm.fdb", "password": "segredo"}


def _rodar(fab, gravar, backup=None, desejadas=("Kolosh", "Sam's Club")):
    saida = []
    cod = tool.executar(
        CFG, gravar=gravar, backup_servidor="C:\\b.fbk", slug="mm",
        desejadas=list(desejadas), conn_factory=fab, backup=backup, out=saida.append,
    )
    return cod, saida


def _ok(cfg, dest):
    return "gbak: closing file, committing, and finishing. 10 bytes written"


def test_dry_run_so_le_e_nao_faz_backup():
    fab = _Fabrica(BASE)
    chamou = []
    cod, saida = _rodar(fab, False, backup=lambda *a: chamou.append(a))
    assert cod == 0 and fab.usadas == ["leitura"] and chamou == []
    assert saida[0] == "Ambiente mm · h:3050 · C:\\mm.fdb"
    assert "Resumo: 0 já existem, 0 parecidas, 2 a inserir." in saida
    assert "Triggers em PRODUTOS_MARCA: nenhum" in saida
    assert saida[-1] == "Dry-run: nada gravado."
    assert not any("segredo" in ln for ln in saida)


def test_conexao_de_leitura_so_recebe_select():
    fab = _Fabrica(BASE)
    _rodar(fab, False)
    assert set(fab.leitura.sqls) == {"SELECT"}


def test_dry_run_avisa_que_gravar_vai_recusar_com_parecida():
    cod, saida = _rodar(_Fabrica([(5, "SAMS CLUB")]), False)
    assert cod == 0 and any("--gravar vai recusar" in ln for ln in saida)


def test_gravar_faz_backup_antes_insere_confere_e_commita():
    fab = _Fabrica(BASE)
    ordem = []

    def bk(cfg, dest):
        ordem.append(("backup", dest, fab.usadas[:]))
        return _ok(cfg, dest)

    cod, saida = _rodar(fab, True, backup=bk)
    assert cod == 0 and ordem == [("backup", "C:\\b.fbk", ["leitura"])]  # backup antes da escrita
    assert fab.escrita.commits == 1 and fab.escrita.rollbacks == 0
    assert (3, "Kolosh") in fab.escrita.linhas and (4, "Sam's Club") in fab.escrita.linhas
    assert any("closing file" in ln for ln in saida)  # prova impressa


def test_gravar_para_se_ha_parecida():
    fab = _Fabrica([(5, "SAMS CLUB")])
    chamou = []
    cod, _ = _rodar(fab, True, backup=lambda *a: chamou.append(a))
    assert cod == 1 and chamou == [] and fab.usadas == ["leitura"]


@pytest.mark.parametrize("kw", [{"triggers": ["TRG_MARCA"]}, {"geradores": ["GEN_MARCA_ID"]}])
def test_gravar_recusa_com_trigger_ou_gerador(kw):
    fab = _Fabrica(BASE, **kw)
    chamou = []
    cod, saida = _rodar(fab, True, backup=lambda *a: chamou.append(a))
    assert cod == 1 and chamou == [] and fab.usadas == ["leitura"]
    assert any("TRG_MARCA" in ln or "GEN_MARCA_ID" in ln for ln in saida)


def test_trigger_inativo_so_avisa():
    fab = _Fabrica(BASE, triggers=[("TRG_MARCA", 1)])
    cod, saida = _rodar(fab, True, backup=_ok)
    assert cod == 0 and "Triggers em PRODUTOS_MARCA: TRG_MARCA (inativo)" in saida


def test_rollback_falhando_nao_deixa_o_commit_de_saida_gravar():
    # divergência na conferência + rollback que cai: a exceção tem que sair do
    # with, senão o commit da saída gravaria as linhas.
    fab = _Fabrica(BASE, esconde=True, falha_rollback=True)
    linhas_antes = list(fab.escrita.linhas)
    cod, saida = _rodar(fab, True, backup=_ok)
    assert cod == 1 and fab.escrita.linhas == linhas_antes
    assert saida[-1].startswith("Gravação falhou e o rollback não confirmou. Resultado incerto")
    assert not any("Rollback feito" in ln for ln in saida)


def test_dry_run_com_trigger_imprime_e_sai_0():
    cod, saida = _rodar(_Fabrica(BASE, triggers=["TRG_MARCA"]), False)
    assert cod == 0 and "Triggers em PRODUTOS_MARCA: TRG_MARCA" in saida


def test_gravar_com_backup_falhando_nao_grava_e_avisa():
    fab = _Fabrica(BASE)

    def falha(cfg, dest):
        raise RuntimeError("sem disco segredo")

    cod, saida = _rodar(fab, True, backup=falha)
    assert cod == 1 and "escrita" not in fab.usadas
    assert any(ln.startswith("Backup falhou (RuntimeError: sem disco ***)") for ln in saida)


def test_backup_falhando_imprime_log_inteiro_mascarado():
    longa = "x" * 300 + " segredo"

    def falha(cfg, dest):
        raise RuntimeError(f"Log sem linha final. Últimas linhas:\n{longa}\nfim")

    cod, saida = _rodar(_Fabrica(BASE), True, backup=falha)
    assert cod == 1 and f"  {'x' * 300} ***" in saida and "  fim" in saida


def test_leitura_falhando_vira_mensagem_clara():
    class Quebrada:
        @contextmanager
        def connect_somente_leitura(self, cfg):
            raise ConnectionError("sem rede")
            yield

    saida = []
    cod = tool.executar(CFG, gravar=False, backup_servidor=None, conn_factory=Quebrada(),
                        out=saida.append)
    assert cod == 1 and any(ln.startswith("Leitura falhou (ConnectionError: sem rede)") for ln in saida)


def test_banco_mudou_entre_leitura_e_escrita_aborta():
    fab = _Fabrica(BASE, linhas_escrita=[*BASE, (3, "Outro")])
    cod, saida = _rodar(fab, True, backup=_ok)
    assert cod == 1 and fab.escrita.rollbacks == 1 and fab.escrita.commits == 0
    assert fab.escrita.pend == [] and any("O banco mudou" in ln for ln in saida)


def test_divergencia_na_conferencia_faz_rollback_e_sai_1():
    fab = _Fabrica(BASE, esconde=True)
    cod, _ = _rodar(fab, True, backup=_ok)
    assert cod == 1 and fab.escrita.rollbacks == 1 and fab.escrita.commits == 0


def test_erro_no_insert_faz_rollback_e_sai_1():
    fab = _Fabrica(BASE, falha_insert=RuntimeError("boom"))
    cod, _ = _rodar(fab, True, backup=_ok)
    assert cod == 1 and fab.escrita.rollbacks == 1 and fab.escrita.commits == 0


def test_colisao_de_pk_pede_para_rodar_de_novo():
    fab = _Fabrica(BASE, falha_insert=RuntimeError("violation of PRIMARY or UNIQUE KEY constraint"))
    cod, saida = _rodar(fab, True, backup=_ok)
    assert cod == 1 and any("Outro cadastro tomou o código; rode de novo" in ln for ln in saida)


def test_commit_falhando_diz_resultado_incerto():
    fab = _Fabrica(BASE, falha_commit=True)
    cod, saida = _rodar(fab, True, backup=_ok)
    assert cod == 1 and any("resultado incerto; rode o dry-run para conferir" in ln for ln in saida)


def test_nada_a_inserir_nao_faz_backup():
    fab = _Fabrica([(3, "Kolosh")])
    chamou = []
    cod, _ = _rodar(fab, True, backup=lambda *a: chamou.append(a), desejadas=["Kolosh"])
    assert cod == 0 and chamou == []


def test_main_gravar_exige_backup_servidor():
    with pytest.raises(SystemExit) as e:
        tool.main(["mm", "--gravar"])
    assert e.value.code == 2


def test_compra_mais_parece_compramais():
    plano = planejar([(5, "COMPRA MAIS")], ["CompraMais"])
    assert plano.parecidas == [("CompraMais", "COMPRA MAIS", 5)]


# --- backup.gbak -------------------------------------------------------------

OK_LOG = ["gbak: readied database", "gbak: closing file, committing, and finishing. 99 bytes written"]


class _Svc:
    def __init__(self, log=None, falha_backup=None, falha_close=False):
        self.calls, self.log = [], OK_LOG if log is None else log
        self.falha_backup, self.falha_close = falha_backup, falha_close

    def backup(self, origem, destino, callback=None):
        self.calls.append(("backup", origem, destino))
        if self.falha_backup:
            raise self.falha_backup
        for ln in self.log:
            callback(ln)

    def wait(self):
        self.calls.append(("wait",))

    def close(self):
        self.calls.append(("close",))
        if self.falha_close:
            raise OSError("close caiu")


class _Services:
    def __init__(self, **kw):
        self.svc, self.kw = _Svc(**kw), None

    def connect(self, **kw):
        self.kw = kw
        return self.svc


CFG_BK = {"host": "srv", "port": "3055", "path": "C:\\Fire\\mm.fdb", "user": "SYSDBA", "password": "x"}


def test_gbak_sucesso_devolve_ultima_linha_e_leva_a_porta():
    s = _Services()
    prova = backup_mod.gbak(CFG_BK, "C:\\bk\\a.fbk", services=s)
    assert s.kw == {"host": "srv/3055", "user": "SYSDBA", "password": "x"}
    assert s.svc.calls == [("backup", "C:\\Fire\\mm.fdb", "C:\\bk\\a.fbk"), ("wait",), ("close",)]
    assert "closing file, committing, and finishing" in prova


def test_gbak_sem_porta_usa_so_o_host():
    s = _Services()
    backup_mod.gbak({**CFG_BK, "port": ""}, "a.fbk", services=s)
    assert s.kw["host"] == "srv"


def test_gbak_porta_invalida():
    with pytest.raises(ValueError):
        backup_mod.gbak({**CFG_BK, "port": "abc"}, "a.fbk", services=_Services())


def test_gbak_log_sem_linha_final_levanta_com_ultimas_linhas():
    s = _Services(log=[f"l{i}" for i in range(8)])
    with pytest.raises(RuntimeError) as e:
        backup_mod.gbak(CFG_BK, "a.fbk", services=s)
    assert "l7" in str(e.value) and "l3" in str(e.value) and "l2" not in str(e.value)


def test_gbak_linha_com_error_levanta_com_a_linha():
    s = _Services(log=["gbak: ERROR: I/O error", *OK_LOG])
    with pytest.raises(RuntimeError, match="erro no log") as e:
        backup_mod.gbak(CFG_BK, "a.fbk", services=s)
    assert "I/O error" in str(e.value)


def test_gbak_tabela_com_error_no_nome_nao_derruba_backup_bom():
    s = _Services(log=["gbak:  writing table LOG_ERROR", *OK_LOG, ""])
    prova = backup_mod.gbak(CFG_BK, "a.fbk", services=s)
    assert "closing file" in prova  # e não a última linha, vazia


def test_gbak_backup_levantando_propaga_e_fecha():
    s = _Services(falha_backup=ConnectionError("caiu"), falha_close=True)
    with pytest.raises(ConnectionError):  # o close que falha não mascara
        backup_mod.gbak(CFG_BK, "a.fbk", services=s)
    assert ("close",) in s.svc.calls


def test_gbak_recusa_embedded():
    with pytest.raises(ValueError):
        backup_mod.gbak({"host": "", "path": "x.fdb"}, "a.fbk", services=_Services())
