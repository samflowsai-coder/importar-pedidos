from __future__ import annotations

import io

import openpyxl
import xlrd

from app.ingestion.file_loader import LoadedFile


class XLSExtractor:
    def extract(self, file: LoadedFile) -> dict:
        if file.extension == ".xlsx":
            return self._extract_xlsx(file.raw)
        return self._extract_xls(file.raw)

    def _make_text(self, rows: list) -> str:
        return " ".join(str(c) for row in rows for c in row if c is not None)

    def _extract_xlsx(self, raw: bytes) -> dict:
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
        rows = []
        sheets = []
        for sheet in wb.worksheets:
            antes = len(rows)
            for row in sheet.iter_rows(values_only=True):
                if any(cell is not None for cell in row):
                    rows.append(list(row))
            sheets.append({"name": sheet.title, "rows": len(rows) - antes})
        return {"rows": rows, "tables": [rows], "text": self._make_text(rows), "sheets": sheets}

    def _extract_xls(self, raw: bytes) -> dict:
        wb = xlrd.open_workbook(file_contents=raw)
        rows = []
        sheets = []
        for sheet in wb.sheets():
            antes = len(rows)
            for i in range(sheet.nrows):
                rows.append(sheet.row_values(i))
            sheets.append({"name": sheet.name, "rows": len(rows) - antes})
        return {"rows": rows, "tables": [rows], "text": self._make_text(rows), "sheets": sheets}
