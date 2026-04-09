from dotenv import load_dotenv

load_dotenv()

import os

from app.exporters.erp_exporter import ERPExporter
from app.ingestion.file_loader import FileLoader
from app.pipeline import process
from app.utils.logger import logger

INPUT_DIR = os.getenv("INPUT_DIR", "input/")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output/")


def main() -> None:
    loader = FileLoader()
    files = loader.load_files(INPUT_DIR)

    if not files:
        logger.warning(f"Nenhum arquivo encontrado em {INPUT_DIR}")
        return

    logger.info(f"Arquivos encontrados: {len(files)}")

    exporter = ERPExporter()
    success = 0

    for file in files:
        order = process(file)
        if order:
            paths = exporter.export(order, OUTPUT_DIR)
            success += 1
            if len(paths) > 1:
                logger.info(f"Pedido {order.header.order_number} dividido em {len(paths)} arquivo(s) por local de entrega")

    logger.info(f"Pipeline concluído: {success}/{len(files)} pedido(s) exportado(s) → {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
