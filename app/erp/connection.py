from __future__ import annotations

import math
import os
import socket
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.erp.exceptions import FirebirdConnectionError
from app.utils.logger import logger


def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# fdb.load_api só pode rodar uma vez por processo; guardamos com este flag.
_fb_lib_loaded = False

# Limite de tempo da CONEXÃO (segundos) para quem abre dentro de
# `limite_de_conexao`; None = sem limite (o padrão do portal inteiro).
_limite_conexao_s: ContextVar[float | None] = ContextVar("fb_limite_conexao_s", default=None)
# Módulo `fdb` em que o hook do DPB já foi registrado (registro é por processo).
_hook_dpb_em: object | None = None


@contextmanager
def limite_de_conexao(segundos: float) -> Iterator[None]:
    """Conexões TCP abertas dentro do bloco (mesma thread/contexto) desistem se
    o servidor não aceitar em `segundos`: sonda TCP antes do `fdb.connect`
    (o limite garantido) e `isc_dpb_connect_timeout` no DPB (o cliente
    Firebird pode respeitar também). Estourou = `FirebirdConnectionError`,
    a mesma falha de "Fire fora". Embedded não tem rede: não muda nada.
    Uso: só o caminho que não pode esperar o timeout do sistema operacional
    (Fire da fonte de preço no `/api/commit`)."""
    token = _limite_conexao_s.set(segundos)
    try:
        yield
    finally:
        _limite_conexao_s.reset(token)


def _dpb_connect_timeout(dsn, dpb) -> None:  # noqa: ARG001 — assinatura do hook do fdb
    """Hook `HOOK_DATABASE_ATTACH_REQUEST`: põe `isc_dpb_connect_timeout` no DPB
    só dentro de `limite_de_conexao`. Devolve None (o fdb segue o connect)."""
    limite = _limite_conexao_s.get()
    if limite is not None:
        import fdb  # type: ignore[import]

        dpb.add_integer_parameter(fdb.isc_dpb_connect_timeout, max(1, math.ceil(limite)))


def _registrar_hook_dpb(fdb) -> None:
    global _hook_dpb_em
    if _hook_dpb_em is not fdb:
        fdb.add_hook(fdb.HOOK_DATABASE_ATTACH_REQUEST, _dpb_connect_timeout)
        _hook_dpb_em = fdb


def _sondar_tcp(host: str, port: int, segundos: float) -> None:
    """O servidor aceita TCP em `segundos`? Senão `FirebirdConnectionError`."""
    try:
        with socket.create_connection((host, port), timeout=segundos):
            pass
    except OSError as exc:
        raise FirebirdConnectionError(
            f"Firebird em {host}:{port} não respondeu em {segundos:g} s: {exc}"
        ) from exc


def _fb_connect_kwargs(cfg: dict) -> dict:
    """Traduz a config do ambiente para os kwargs de `fdb.connect`.

    `host` presente → TCP (host/port + database = caminho no SERVIDOR); ausente
    → embedded (só database). Passamos os campos SEPARADOS (forma idiomática do
    `fdb`) em vez de montar um DSN `host/port:C:\\...` na mão — mais limpo, e o
    `fdb` remonta a string interna sozinho.

    Levanta FirebirdConnectionError se a porta não for numérica (o campo em
    /admin/ambientes é texto livre; sem isto o ValueError escaparia cru, fora do
    contrato de erro que o resto do app espera).
    """
    kwargs: dict = {
        "database": (cfg.get("path") or "").strip(),
        "user": (cfg.get("user") or "SYSDBA").strip(),
        "password": cfg.get("password") or "",
        "charset": (cfg.get("charset") or "WIN1252").strip(),
    }
    host = (cfg.get("host") or "").strip()
    if host:
        kwargs["host"] = host
        port_raw = (cfg.get("port") or "3050").strip() or "3050"
        try:
            kwargs["port"] = int(port_raw)
        except ValueError as exc:
            raise FirebirdConnectionError(f"Porta do Firebird inválida: {port_raw!r}") from exc
    return kwargs


class FirebirdConnection:
    """
    Manages Firebird connections in two modes:

    Embedded (local file):
        FB_DATABASE=/path/to/empresa.fdb

    TCP (remote server):
        FB_HOST=192.168.1.10
        FB_PORT=3050  (default)
        FB_DATABASE=C:\\Fire\\empresa.fdb  (path on the server)

    Context manager commits on clean exit, rolls back on any exception.
    """

    def is_configured(self) -> bool:
        return bool(_get_env("FB_DATABASE"))

    @staticmethod
    def _configure_library() -> None:
        """Carrega uma fbclient específica se FB_CLIENT_LIBRARY estiver setado.

        No cliente (Fire Sistemas) NÃO setamos isso: o `fdb` encontra sozinho a
        fbclient do Firebird 2.5 já instalado. A env var fica como escape para
        apontar uma DLL isolada (ex.: cliente moderno numa pasta própria).
        `fdb.load_api` só roda uma vez por processo — guardado pelo flag.
        """
        global _fb_lib_loaded
        lib = _get_env("FB_CLIENT_LIBRARY")
        if lib and not _fb_lib_loaded:
            import fdb  # type: ignore[import]
            try:
                fdb.load_api(lib)
            except Exception as exc:  # noqa: BLE001 — nunca deve derrubar o connect
                logger.warning(f"FB_CLIENT_LIBRARY: não foi possível carregar {lib}: {exc}")
            _fb_lib_loaded = True

    @contextmanager
    def connect(self) -> Generator:
        """Conexão usando env vars (legado)."""
        cfg = {
            "path": _get_env("FB_DATABASE"),
            "host": _get_env("FB_HOST"),
            "port": _get_env("FB_PORT", "3050"),
            "user": _get_env("FB_USER", "SYSDBA"),
            "password": _get_env("FB_PASSWORD", "masterkey"),
            "charset": _get_env("FB_CHARSET", "WIN1252"),
        }
        with self._connect_with(cfg) as conn:
            yield conn

    @contextmanager
    def connect_with_config(self, cfg: dict) -> Generator:
        """Conexão usando config explícita (multi-ambiente).

        cfg: dict com keys path, host, port, user, password, charset.
        Strings vazias em host/port viram embedded.
        """
        with self._connect_with(cfg) as conn:
            yield conn

    @contextmanager
    def connect_somente_leitura(self, cfg: dict) -> Generator:
        """Conexão para LER o ERP (vigia SP1, medições): transação READ ONLY +
        SNAPSHOT (isc_tpb_read + isc_tpb_concurrency), NOWAIT.

        - READ ONLY: o próprio Firebird recusa qualquer escrita nesta
          transação — a garantia não depende de o SQL estar certo.
        - SNAPSHOT: cabeçalhos, itens e notas lidos na mesma fotografia do
          banco; um faturamento no meio da leitura não aparece pela metade.

        Passa por `_connect_with` (a cerca de testes `_no_real_firebird`
        continua valendo); o commit de saída de uma transação READ ONLY não
        grava nada.
        """
        import fdb  # type: ignore[import]

        tpb = bytes([fdb.isc_tpb_version3, fdb.isc_tpb_read, fdb.isc_tpb_concurrency, fdb.isc_tpb_nowait])
        with self._connect_with(cfg) as conn:
            conn.begin(tpb=tpb)
            yield conn

    @contextmanager
    def connect_leitura_bloqueante(self, cfg: dict, lock_timeout: int = 10) -> Generator:
        """Conexão para LER provas de ausência (recuperação do cadastro): WRITE +
        READ COMMITTED + NO_REC_VERSION, WAIT com `isc_tpb_lock_timeout`.

        Com REC_VERSION (ou SNAPSHOT), um INSERT sem commit de outra transação
        simplesmente não aparece e "não existe" pareceria provado. Com
        NO_REC_VERSION, ler um registro cuja última versão está pendente espera e,
        vencido o timeout, levanta conflito de lock: quem chama aborta, não marca.

        WRITE e não READ ONLY: uma transação READ ONLY + READ COMMITTED pode ser
        tratada pelo servidor como pré-comitada e ler a última versão comitada,
        ignorando o NO_REC_VERSION (não provado no Firebird 2.5 do Fire: fica para a
        P0). Nada é escrito por dois caminhos: quem lê passa pelo guarda de SELECT
        (`cadastro_recuperacao.SoSelect`), e a saída é SEMPRE rollback, nunca commit.
        """
        import struct

        import fdb  # type: ignore[import]

        tpb = bytes([
            fdb.isc_tpb_version3, fdb.isc_tpb_write, fdb.isc_tpb_read_committed,
            fdb.isc_tpb_no_rec_version, fdb.isc_tpb_wait, fdb.isc_tpb_lock_timeout,
        ]) + struct.pack("<bI", 4, lock_timeout)
        with self._connect_with(cfg, commit_na_saida=False) as conn:
            conn.main_transaction.default_tpb = tpb
            conn.main_transaction.default_action = "rollback"
            conn.begin(tpb=tpb)
            yield conn

    @contextmanager
    def connect_escrita_cadastro(self, cfg: dict, lock_timeout: int = 10) -> Generator:
        """Conexão de ESCRITA do cadastro de produto (volta Flow -> Fire):
        READ COMMITTED + REC_VERSION, WAIT com `isc_tpb_lock_timeout` de
        `lock_timeout` segundos.

        Sem o timeout, um SEQ aberto na tela do Fire deixaria o gravador
        esperando o lock sem limite (e com ele o `_lock` do processo). O TPB
        vai também como `default_tpb` da transação principal: o fdb abre a
        transação seguinte a cada commit/rollback com esse padrão, então
        todo cadastro do lote roda com o mesmo timeout.

        A saída é SEMPRE rollback, nunca commit: o gravador comita cada
        cadastro sozinho, e o que sobra pendente (um rollback que falhou no
        meio) não tem prova de nada.
        """
        import struct

        import fdb  # type: ignore[import]

        tpb = bytes([
            fdb.isc_tpb_version3, fdb.isc_tpb_write, fdb.isc_tpb_read_committed,
            fdb.isc_tpb_rec_version, fdb.isc_tpb_wait, fdb.isc_tpb_lock_timeout,
        ]) + struct.pack("<bI", 4, lock_timeout)
        with self._connect_with(cfg, commit_na_saida=False) as conn:
            conn.main_transaction.default_tpb = tpb
            # Por padrão o fdb comita a transação ativa quando ela é encerrada sem
            # commit/rollback explícito (`begin()` de uma nova, `Transaction.close()`).
            conn.main_transaction.default_action = "rollback"
            conn.begin(tpb=tpb)
            yield conn

    @contextmanager
    def _connect_with(self, cfg: dict, *, commit_na_saida: bool = True) -> Generator:
        # fdb (driver legado) — compatível com o cliente Firebird 2.5 do Fire
        # Sistemas. O firebird-driver (moderno) exige fbclient 3.0+, que os
        # servidores 2.5 do cliente não têm.
        import fdb  # type: ignore[import]
        self._configure_library()

        kwargs = _fb_connect_kwargs(cfg)
        if not kwargs["database"]:
            raise FirebirdConnectionError("Caminho do Firebird (.fdb) não configurado.")

        mode = f"TCP {kwargs['host']}" if "host" in kwargs else "embedded"
        logger.debug(
            f"Conectando ao Firebird: {mode} → {kwargs['database']} "
            f"[charset={kwargs['charset']}]"
        )

        limite = _limite_conexao_s.get()
        if limite is not None and "host" in kwargs:
            _sondar_tcp(kwargs["host"], kwargs["port"], limite)
            _registrar_hook_dpb(fdb)

        try:
            conn = fdb.connect(**kwargs)
        except Exception as exc:
            raise FirebirdConnectionError(f"Falha ao conectar ao Firebird: {exc}") from exc

        try:
            yield conn
            if commit_na_saida:
                conn.commit()
            else:
                conn.rollback()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
