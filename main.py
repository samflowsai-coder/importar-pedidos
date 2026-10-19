from dotenv import load_dotenv

load_dotenv()

import os
import sys

from app import config as app_config
from app.exporters.erp_exporter import ERPExporter
from app.ingestion.file_loader import FileLoader
from app.pipeline import process
from app.utils.logger import logger

INPUT_DIR = os.getenv("INPUT_DIR", "input/")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output/")


def main() -> None:
    cfg = app_config.load()
    export_mode = cfg.get("export_mode", "xlsx")

    if export_mode in ("db", "both"):
        sys.exit("Gravação direta no Fire só pelo portal (preview → enviar).")

    xlsx_exporter: ERPExporter | None = ERPExporter() if export_mode in ("xlsx", "both") else None

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
