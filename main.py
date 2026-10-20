from dotenv import load_dotenv

load_dotenv()

import os
import sys

from app import config as app_config
from app.erp import depara_apply, produto_trava
from app.erp.product_check import check_order
from app.exporters.erp_exporter import ERPExporter
from app.ingestion.file_loader import FileLoader
from app.pipeline import process
from app.utils.logger import logger

INPUT_DIR = os.getenv("INPUT_DIR", "input/")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output/")


# Trava de produto no CLI: só com PORTAL_PRODUTO_CHAVE_CLI=ligado. Ausente ou
# qualquer outro valor = comportamento de hoje (nem chama o check_order).
_ENV_TRAVA_CLI = "PORTAL_PRODUTO_CHAVE_CLI"


def _trava_produto_cli_ligada() -> bool:
    return os.getenv(_ENV_TRAVA_CLI) == "ligado"


def _item_sem_produto_exato(order) -> bool:
    """True (e loga) se o pedido não pode ir à planilha: mesma regra do
    `produto_trava.avaliar` em `ligado`. Falha no check pula só este pedido.

    Liberado, aplica no `order` o produto decidido pelo MESMO check (a planilha
    leva o `CODPROD_ALTERN` dele, com o código do cliente na OBS)."""
    numero = order.header.order_number
    try:
        check = check_order(order)
        tp = produto_trava.avaliar(check, env={"produto_chave_modo": "ligado"}, export_mode="xlsx")
    except Exception as exc:  # noqa: BLE001 — um pedido não derruba o lote
        logger.warning(
            f"Pedido {numero} não exportado: falha ao conferir o produto ({type(exc).__name__})."
        )
        return True
    if not tp.bloqueado:
        depara_apply.aplicar(order, produto_trava.produtos_decididos(check)[0])
        return False
    if tp.reason == "fire_indisponivel":
        logger.warning(
            f"Pedido {numero} não exportado: o produto dos itens não pôde ser conferido "
            "(Fire indisponível ou vínculos ilegíveis)."
        )
        return True
    primeiro = tp.itens[0] if tp.itens else {"index": 0, "motivo": None}
    motivo = (primeiro.get("motivo") or "produto não identificado no Fire").rstrip(".")
    problema = (
        "sem produto exato" if tp.reason == "produto_a_identificar" else "não pode ir na planilha"
    )
    logger.warning(
        f"Pedido {numero} não exportado: item {primeiro['index'] + 1} {problema} ({motivo})."
    )
    return True


def main() -> None:
    cfg = app_config.load()
    export_mode = cfg.get("export_mode", "xlsx")

    if export_mode in ("db", "both"):
        sys.exit("Gravação direta no Fire só pelo portal (preview → enviar).")

    xlsx_exporter: ERPExporter | None = ERPExporter() if export_mode in ("xlsx", "both") else None

    trava_produto = _trava_produto_cli_ligada()
    loader = FileLoader()
    files = loader.load_files(INPUT_DIR)

    logger.info("Portal de Pedidos — iniciando processamento em lote")

    if not files:
        logger.warning(f"Nenhum arquivo encontrado em {INPUT_DIR}")
        return

    logger.info(f"Arquivos encontrados: {len(files)} | modo de exportação: {export_mode}")

    success = 0

    for file in files:
        order = process(file)
        if not order:
            continue

        # Produto por chave exata (só com PORTAL_PRODUTO_CHAVE_CLI=ligado): item
        # sem produto exato não vai para a planilha (o CLI não tem revisão).
        if xlsx_exporter and trava_produto and _item_sem_produto_exato(order):
            continue

        success += 1

        if xlsx_exporter:
            paths = xlsx_exporter.export(order, OUTPUT_DIR)
            if len(paths) > 1:
                logger.info(
                    f"Pedido {order.header.order_number} dividido em {len(paths)} arquivo(s) por local de entrega"
                )

    logger.info(f"Pipeline concluído: {success}/{len(files)} pedido(s) processado(s) → {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
