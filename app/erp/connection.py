from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager

from app.erp.exceptions import FirebirdConnectionError
from app.utils.logger import logger


def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


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
        """Load non-default Firebird client library if FB_CLIENT_LIBRARY is set."""
        lib = _get_env("FB_CLIENT_LIBRARY")
        if lib:
            from firebird.driver import driver_config  # type: ignore[import]
            driver_config.fb_client_library.value = lib

    @contextmanager
    def connect(self) -> Generator:
        from firebird.driver import connect  # type: ignore[import]
        self._configure_library()

        database = _get_env("FB_DATABASE")
        if not database:
            raise FirebirdConnectionError("FB_DATABASE não configurado.")

        host = _get_env("FB_HOST")
        port = int(_get_env("FB_PORT", "3050"))
        user = _get_env("FB_USER", "SYSDBA")
        password = _get_env("FB_PASSWORD", "masterkey")
        charset = _get_env("FB_CHARSET", "WIN1252")

        mode = f"TCP {host}" if host else "embedded"
        logger.debug(f"Conectando ao Firebird: {mode} → {database} [charset={charset}]")

        try:
            if host:
                conn = connect(
                    host=host, port=port, database=database,
                    user=user, password=password, charset=charset,
                )
            else:
                conn = connect(
                    database=database, user=user, password=password, charset=charset,
                )
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
