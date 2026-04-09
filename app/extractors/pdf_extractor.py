import io

import pdfplumber

from app.ingestion.file_loader import LoadedFile


class PDFExtractor:
    def extract(self, file: LoadedFile) -> dict:
        text_pages = []
        tables = []
        with pdfplumber.open(io.BytesIO(file.raw)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    text_pages.append(text)
                page_tables = page.extract_tables()
                if page_tables:
                    tables.extend(page_tables)
        return {
            "text": "\n".join(text_pages),
            "tables": tables,
        }
