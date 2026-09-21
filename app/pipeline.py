from __future__ import annotations

import hashlib

from app.classifiers.format_classifier import FileFormat, FormatClassifier
from app.extractors.pdf_extractor import PDFExtractor
from app.extractors.xls_extractor import XLSExtractor
from app.ingestion.file_loader import LoadedFile
from app.llm.fallback_parser import LLMFallbackParser
from app.models.order import Order
from app.normalizers.order_normalizer import OrderNormalizer
from app.parsers.beira_rio_parser import BeiranRioParser
from app.parsers.daju_parser import DajuParser
from app.parsers.desmembramento_xls_parser import DesmembramentoXlsParser
from app.parsers.generic_parser import GenericParser
from app.parsers.kallan_xls_parser import KallanXlsParser
from app.parsers.kolosh_parser import KoloshParser
from app.parsers.mercado_eletronico_parser import MercadoEletronicoParser
from app.parsers.nasmar_template_parser import NasmarTemplateParser
from app.parsers.pedido_compras_revenda_parser import PedidoComprasRevendaParser
from app.parsers.sams_club_parser import SamsClubParser
from app.parsers.sbf_centauro_parser import SbfCentauroParser
from app.routing import documento
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
    NasmarTemplateParser(),
    DajuParser(),
    DesmembramentoXlsParser(),
    GenericParser(),
]
_normalizer = OrderNormalizer()
_validator = OrderValidator()
_llm = LLMFallbackParser()


def process(file: LoadedFile) -> Order | None:
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
    _marcar_fornecedor(order, extracted)
    _numerar_se_ausente(order, file.raw)
    order = _normalizer.normalize(order)
    _validator.validate(order)

    logger.info(f"Pedido {order.header.order_number!r} → {len(order.items)} item(s)")
    return order


def _numerar_se_ausente(order: Order, raw: bytes) -> None:
    """Pedido sem número no documento recebe `SN-` + 8 hex do sha256 do arquivo.

    O `order_number` vira `PEDIDO_CLIENTE` no Fire, que o copia para a nota
    fiscal (xPed, cortado em 15 — por isso 11 caracteres). Em modo xlsx é o
    único campo que liga o pedido do portal à linha do Fire: sem ele o pedido
    fica órfão, sem reconciliação. Parser nunca inventa número; o portal gera
    aqui, marca `order_number_gerado` e o preview avisa.

    Determinístico: o mesmo arquivo dá o mesmo número (reimport, preview de
    novo, worker), e é o prefixo do `imports.file_sha256` — do número impresso
    na nota o suporte chega no import. O hífen seguido de 8 caracteres nunca
    casa o corte de sufixo de loja da reconciliação (`app/erp/numero_pedido.py`).
    """
    if order.header.order_number:
        return
    numero = f"SN-{hashlib.sha256(raw).hexdigest()[:8].upper()}"
    order.header.order_number = numero
    order.header.order_number_gerado = True
    logger.warning(f"Documento sem número de pedido — número gerado pelo portal: {numero}")


def _marcar_fornecedor(order: Order, extracted: dict) -> None:
    """Preenche `header.supplier_cnpj` pela varredura do texto do documento.

    Um parser que já tenha lido o rótulo FORNECEDOR ganha: só preenche se o
    campo estiver vazio.

    Nunca levanta. Um erro aqui (banco compartilhado ausente no CLI, ambiente
    sem CNPJ cadastrado) tem que deixar o pedido passar sem fornecedor — o
    roteamento cai para o degrau seguinte, que é exatamente o desenho. Parsing
    não pode quebrar por causa de roteamento.
    """
    if order.header.supplier_cnpj:
        return
    try:
        conhecidos = documento.cnpjs_de_ambientes()
        if not conhecidos:
            return
        order.header.supplier_cnpj = documento.detectar_fornecedor(
            extracted.get("text", "") or "", conhecidos
        )
    except Exception as exc:  # noqa: BLE001 — roteamento nunca derruba parsing
        logger.debug(f"fornecedor não detectado ({exc!r}); segue sem supplier_cnpj")
