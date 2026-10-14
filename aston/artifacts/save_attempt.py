
from pathlib import Path


def save_code(path: Path, source_code: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source_code, encoding="utf-8")
