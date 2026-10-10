"""produtoProdutor: item da base intermediária → produto da base produtora."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.erp_eventos import produtor
from app.erp_eventos.modelo import ItemErp, PedidoErp
from app.persistence import context as env_context
from app.persistence import db, environments_repo, erp_bases_repo, erp_vinculo_repo, repo, router
from app.security import secret_store

TENANT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
AGORA = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)


class CatalogoFake:
    """EAN 789 → SEQ 2613 (único); código KIT-1 em dois produtos (ambíguo); código M1 → 77."""

    def __init__(self):
        self.consultas = 0

    @contextmanager
    def connect_somente_leitura(self, cfg):
        fake = self

        class Cur:
            def execute(self, sql, params):
                fake.consultas += 1
                if "EAN13" in sql:
                    self._r = [(p, 2613, "x", 0) for p in params if p == "789"]
                else:
                    self._r = (
                        [("KIT-1", 1, "a", 0), ("KIT-1", 2, "b", 0)] if "KIT-1" in params else []
                    )
                    self._r += [("M1", 77, "m", 0)] if "M1" in params else []

            def fetchall(self):
                return self._r

            def close(self):
                pass

        class Conn:
            def cursor(self):
                return Cur()

        yield Conn()


@pytest.fixture
def bases(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(secret_store, "_KEY_FILE", tmp_path / ".secret.key")
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    cfgs = {}
    for slug, papel in (("mm", "produtora"), ("nasmar", "intermediaria")):
        env = environments_repo.create(
            slug=slug,
            name=slug,
            watch_dir=str(tmp_path / slug),
            output_dir=str(tmp_path / slug),
            fb_path="x",
        )
        erp_bases_repo.salvar(
            env["id"],
            base=slug,
            papel=papel,
            tenant_id=TENANT,
            destino_url="https://flow.test",
            foto_hora=None,
            ativo=True,
            token="fpe_" + "f" * 43,
        )
        cfgs[slug] = erp_bases_repo.obter(env["id"])
    catalogo = CatalogoFake()
    monkeypatch.setattr(produtor, "FirebirdConnection", lambda: catalogo)
    yield cfgs, catalogo
    db.set_db_path(None)
    db.reset_init_cache()


def _pedido(*codigos):
    itens = tuple(
        ItemErp(c, 9, None, "x", Decimal(1), Decimal(0), None, None, None, None) for c in codigos
    )
    return PedidoErp(1, "P", 1, None, None, "PEDIDO", None, None, itens)


def _import(cfg, itens):
    with env_context.active_env(cfg.environment_id, cfg.env_slug):
        repo.insert_import(
            {
                "id": "imp",
                "source_filename": "a.pdf",
                "imported_at": "2026-10-01T09:00:00",
                "order_number": "P",
                "customer": "C",
                "customer_cnpj": None,
                "status": "success",
                "error": None,
                "portal_status": "parsed",
                "snapshot": {"header": {"order_number": "P"}, "items": itens},
            }
        )


def test_base_produtora_usa_o_proprio_produto(bases):
    cfgs, _ = bases
    assert produtor.resolver(cfgs["mm"], [_pedido(10)]) == {10: 9}


def test_intermediaria_resolve_unico_por_ean_depois_codigo_e_nunca_ambiguo(bases):
    cfgs, catalogo = bases
    nas = cfgs["nasmar"]
    _import(
        nas,
        [{"ean": "789", "product_code": "X"}, {"product_code": "KIT-1"}, {"product_code": "M1"}],
    )
    with router.env_connect("nasmar") as conn:
        erp_vinculo_repo.gravar_vinculos(
            conn,
            base="nasmar",
            erp_codigo=1,
            import_id="imp",
            pares=[(10, 0), (11, 1), (12, 2)],
            origem="xlsx",
            agora="2026-10-09T12:00:00.000+00:00",
        )
    r = produtor.resolver(nas, [_pedido(10, 11, 12, 13)], agora=AGORA)
    assert r == {10: 2613, 11: None, 12: 77}  # 13 sem vínculo → fora (None no envio)
    consultas = catalogo.consultas
    assert produtor.resolver(nas, [_pedido(10, 12)], agora=AGORA) == {10: 2613, 12: 77}
    assert catalogo.consultas == consultas  # resolvido fica guardado, não consulta de novo
