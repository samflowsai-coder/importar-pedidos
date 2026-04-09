from __future__ import annotations

from typing import Optional

from app.classifiers.format_classifier import FileFormat, FormatClassifier
from app.extractors.pdf_extractor import PDFExtractor
from app.extractors.xls_extractor import XLSExtractor
from app.ingestion.file_loader import LoadedFile
from app.llm.fallback_parser import LLMFallbackParser
from app.models.order import Order
from app.normalizers.order_normalizer import OrderNormalizer
from app.parsers.beira_rio_parser import BeiranRioParser
from app.parsers.desmembramento_xls_parser import DesmembramentoXlsParser
from app.parsers.generic_parser import GenericParser
from app.parsers.kallan_xls_parser import KallanXlsParser
from app.parsers.kolosh_parser import KoloshParser
from app.parsers.mercado_eletronico_parser import MercadoEletronicoParser
from app.parsers.pedido_compras_revenda_parser import PedidoComprasRevendaParser
from app.parsers.sams_club_parser import SamsClubParser
from app.parsers.sbf_centauro_parser import SbfCentauroParser
from app.utils.logger import logger
from app.validators.order_validator import OrderValidator

_classifier = FormatClassifier()
_pdf_extractor = PDFExtractor()
_xls_extractor = XLSExtractor()
_parsers = [
    MercadoEletronicoParser(),
    PedidoComprasRevendaParser(),
    SbfCentauroParser(),
    BeiranRioParser(),
    KoloshParser(),
    SamsClubParser(),
    KallanXlsParser(),
    DesmembramentoXlsParser(),
    GenericParser(),
]
_normalizer = OrderNormalizer()
_validator = OrderValidator()
_llm = LLMFallbackParser()


def process(file: LoadedFile) -> Optional[Order]:
    logger.info(f"Processando: {file.path.name}")

    fmt = _classifier.classify(file)
    if fmt == FileFormat.UNKNOWN:
        logger.warning(f"Formato desconhecido, ignorando: {file.path.name}")
        return None

    extracted = _pdf_extractor.extract(file) if fmt == FileFormat.PDF else _xls_extractor.extract(file)

    order = None
    for parser in _parsers:
        if hasattr(parser, "can_parse") and not parser.can_parse(extracted):
            continue
        order = parser.parse(extracted)
        if order is not None:
            logger.debug(f"Parser {parser.__class__.__name__} extraiu o pedido")
            break

    if order is None:
        logger.info(f"Parsers sem resultado, ativando LLM fallback: {file.path.name}")
        order = _llm.parse(extracted, source_file=str(file.path))

    if order is None:
        logger.error(f"Não foi possível extrair pedido de: {file.path.name}")
        return None

    order.source_file = str(file.path)
    order = _normalizer.normalize(order)
    _validator.validate(order)

    logger.info(f"Pedido {order.header.order_number!r} → {len(order.items)} item(s)")
    return order
