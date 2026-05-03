import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import Settings

_UTF8_BOM = b"\xef\xbb\xbf"


class Utf8BomRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        if os.name == "nt":
            _ensure_utf8_bom(Path(self.baseFilename))
        return super()._open()


def configure_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(settings.log_level.upper())

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    file_handler = Utf8BomRotatingFileHandler(
        settings.log_dir / "jarvis.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)


def _ensure_utf8_bom(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        path.write_bytes(_UTF8_BOM)
        return
    with path.open("rb") as handle:
        prefix = handle.read(len(_UTF8_BOM))
    if prefix == _UTF8_BOM:
        return
    path.write_bytes(_UTF8_BOM + path.read_bytes())
