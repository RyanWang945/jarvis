from app.logging_config import _UTF8_BOM, _ensure_utf8_bom


def test_ensure_utf8_bom_preserves_existing_log_content(tmp_path) -> None:
    log_path = tmp_path / "jarvis.log"
    content = "2026-05-03 INFO 中文日志\n".encode("utf-8")
    log_path.write_bytes(content)

    _ensure_utf8_bom(log_path)

    assert log_path.read_bytes() == _UTF8_BOM + content


def test_ensure_utf8_bom_is_idempotent(tmp_path) -> None:
    log_path = tmp_path / "jarvis.log"
    content = _UTF8_BOM + "已有 BOM\n".encode("utf-8")
    log_path.write_bytes(content)

    _ensure_utf8_bom(log_path)

    assert log_path.read_bytes() == content
