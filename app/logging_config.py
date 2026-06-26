import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.observability.tracing import current_trace_ids
from app.config import Settings

_UTF8_BOM = b"\xef\xbb\xbf"


class Utf8BomRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        if os.name == "nt":
            _ensure_utf8_bom(Path(self.baseFilename))
        return super()._open()


class TraceContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        trace_id, span_id = current_trace_ids()
        record.otel_trace_id = trace_id or "-"
        record.otel_span_id = span_id or "-"
        return True


def configure_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(settings.log_level.upper())

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] [trace_id=%(otel_trace_id)s span_id=%(otel_span_id)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    trace_filter = TraceContextFilter()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(trace_filter)

    file_handler = Utf8BomRotatingFileHandler(
        settings.log_dir / "jarvis.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(trace_filter)

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
