#!/usr/bin/env python3
"""Create or reset a Portal user. Reads password from a TTY prompt or stdin.

Usage:
    .venv/bin/python tools/create_user.py admin@portal.local --role admin
    # prompts for password twice (hidden), creates user

    echo "supersecret" | .venv/bin/python tools/create_user.py bot@portal.local
    # reads password from stdin (single line); for non-interactive bootstrap

    .venv/bin/python tools/create_user.py admin@portal.local --reset
    # if user exists, replaces password instead of creating

Bootstrap the first admin like this. There is no signup flow on the UI by
design — accounts are created out-of-band.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from app.persistence import db, users_repo
from app.persistence.users_repo import (
    VALID_ROLES,
    DuplicateEmailError,
    InvalidRoleError,
)
from app.security.passwords import (
    PasswordTooLongError,
    WeakPasswordError,
    hash_password,
)


def _read_password(*, confirm: bool) -> str:
    """Reads from TTY if interactive (no echo), else from stdin (one line)."""
    if sys.stdin.isatty():
        pw = getpass.getpass("Senha: ")
        if confirm:
            again = getpass.getpass("Confirme a senha: ")
            if pw != again:
                print("ERRO: senhas não conferem.", file=sys.stderr)
                sys.exit(2)
        return pw
    raw = sys.stdin.readline()
    if not raw:
        print("ERRO: stdin vazio (esperava senha).", file=sys.stderr)
        sys.exit(2)
    return raw.rstrip("\n")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Criar ou resetar usuário do Portal de Pedidos.",
    )
    p.add_argument("email", help="endereço de e-mail (será normalizado para minúsculas)")
    p.add_argument(
        "--role", default="operator", choices=sorted(VALID_ROLES),
        help="papel do usuário (default: operator)",
    )
    p.add_argument(
        "--reset", action="store_true",
        help="se o usuário já existir, substitui a senha em vez de erro",
    )
    args = p.parse_args()

    db.init()

    existing = users_repo.find_by_email(args.email)
    if existing and not args.reset:
        print(
            f"ERRO: usuário {args.email} já existe. Use --reset para trocar a senha.",
            file=sys.stderr,
        )
        return 2

    password = _read_password(confirm=not args.reset)

    try:
        if existing:
            new_hash = hash_password(password)
            users_repo.update_password_hash(existing.id, new_hash)
            print(f"OK: senha resetada para {existing.email} (id={existing.id})")
        else:
            user = users_repo.create_user(
                email=args.email, password=password, role=args.role,
            )
            print(f"OK: usuário criado {user.email} (id={user.id}, role={user.role})")
    except WeakPasswordError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    except PasswordTooLongError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    except DuplicateEmailError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    except InvalidRoleError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
