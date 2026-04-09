from enum import Enum

from app.ingestion.file_loader import LoadedFile


class FileFormat(str, Enum):
    PDF = "pdf"
    XLS = "xls"
    XLSX = "xlsx"
    UNKNOWN = "unknown"


_EXT_MAP = {
    ".pdf": FileFormat.PDF,
    ".xls": FileFormat.XLS,
    ".xlsx": FileFormat.XLSX,
}


class FormatClassifier:
    def classify(self, file: LoadedFile) -> FileFormat:
        return _EXT_MAP.get(file.extension, FileFormat.UNKNOWN)
