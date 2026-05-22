from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest


def pytest_configure(config) -> None:
    if getattr(config.option, "basetemp", None):
        return

    basetemp = Path.cwd() / ".pytest_tmp" / f"run-{os.getpid()}"
    basetemp.parent.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(basetemp)
    os.environ.setdefault("TMP", str(basetemp))
    os.environ.setdefault("TEMP", str(basetemp))
    tempfile.tempdir = str(basetemp)


@pytest.fixture
def tmp_path(request) -> Path:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name).strip("_") or "test"
    path = Path.cwd() / ".pytest_tmp_cases" / f"{name}-{uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
