from dataclasses import dataclass
from pathlib import Path


@dataclass
class LoadedFile:
    path: Path
    extension: str
    raw: bytes


class FileLoader:
    SUPPORTED = {".pdf", ".xls", ".xlsx"}

    def load_files(self, directory: str) -> list[LoadedFile]:
        base = Path(directory)
        if not base.exists():
            return []
        files = []
        for path in sorted(base.iterdir()):
            if path.suffix.lower() in self.SUPPORTED and path.is_file():
                files.append(
                    LoadedFile(
                        path=path,
                        extension=path.suffix.lower(),
                        raw=path.read_bytes(),
                    )
                )
        return files
