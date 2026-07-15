from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager

from app.erp.exceptions import FirebirdConnectionError
from app.utils.logger import logger


def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# fdb.load_api só pode rodar uma vez por processo; guardamos com este flag.
_fb_lib_loaded = False


def _fb_connect_kwargs(cfg: dict) -> dict:
    """Traduz a config do ambiente para os kwargs de `fdb.connect`.

    `host` presente → TCP (host/port + database = caminho no SERVIDOR); ausente
    → embedded (só database). Passamos os campos SEPARADOS em vez de um DSN
    `host/port:C:\\...` de propósito: o drive-letter do Windows (`C:`) faz o
    parser de DSN tropeçar no 2º `:`. Com kwargs separados não há ambiguidade.
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
        kwargs["port"] = int((cfg.get("port") or "3050").strip() or "3050")
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
    def _connect_with(self, cfg: dict) -> Generator:
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

        try:
            conn = fdb.connect(**kwargs)
        except Exception as exc:
            raise FirebirdConnectionError(f"Falha ao conectar ao Firebird: {exc}") from exc

        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
