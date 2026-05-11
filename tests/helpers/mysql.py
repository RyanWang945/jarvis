from __future__ import annotations

import os
import re
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from app.api.agent import get_conversation_store
from app.config import get_settings


DEFAULT_TEST_DATABASE = "jarvis_test"
_DATABASE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


def prepare_test_mysql_database(monkeypatch) -> str:
    """Recreate the isolated MySQL database used by API/runtime tests."""
    database = os.environ.get("JARVIS_TEST_MYSQL_DATABASE", DEFAULT_TEST_DATABASE)
    _validate_database_name(database)

    monkeypatch.setenv("JARVIS_MYSQL_DATABASE", database)
    get_settings.cache_clear()
    _recreate_database(database)

    get_conversation_store.cache_clear()
    get_settings.cache_clear()
    return database


def _recreate_database(database: str) -> None:
    settings = get_settings()
    admin_user = os.environ.get("JARVIS_TEST_MYSQL_ROOT_USER", "root")
    admin_password = os.environ.get("JARVIS_TEST_MYSQL_ROOT_PASSWORD", "jarvis_root")
    app_user = os.environ.get("JARVIS_MYSQL_USER", settings.mysql_user)
    app_password = os.environ.get("JARVIS_MYSQL_PASSWORD", settings.mysql_password)

    engine = create_engine(
        URL.create(
            "mysql+pymysql",
            username=admin_user,
            password=admin_password,
            host=settings.mysql_host,
            port=settings.mysql_port,
            query={"charset": "utf8mb4"},
        ),
        pool_pre_ping=True,
    )
    try:
        with engine.begin() as conn:
            quoted_database = _quote_identifier(database)
            conn.execute(sa.text(f"DROP DATABASE IF EXISTS {quoted_database}"))
            conn.execute(
                sa.text(
                    f"CREATE DATABASE {quoted_database} "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )
            )
            _ensure_app_user(conn, app_user, app_password, host="%")
            _ensure_app_user(conn, app_user, app_password, host="localhost")
            conn.execute(sa.text(f"GRANT ALL PRIVILEGES ON {quoted_database}.* TO {_quote_user(app_user, '%')}"))
            conn.execute(sa.text(f"GRANT ALL PRIVILEGES ON {quoted_database}.* TO {_quote_user(app_user, 'localhost')}"))
            conn.execute(sa.text("FLUSH PRIVILEGES"))
            conn.execute(sa.text(f"USE {quoted_database}"))
            for statement in _schema_statements():
                conn.execute(sa.text(statement))
    finally:
        engine.dispose()


def _schema_statements() -> list[str]:
    root = Path(__file__).resolve().parents[2]
    schema_files = [
        root / "scripts" / "mysql-init" / "001_v1_schema.sql",
        root / "scripts" / "mysql-init" / "003_scheduler_schema.sql",
    ]
    statements: list[str] = []
    for schema_file in schema_files:
        sql = schema_file.read_text(encoding="utf-8")
        sql = re.sub(r"(?m)^\s*--.*$", "", sql)
        for raw in sql.split(";"):
            statement = raw.strip()
            if not statement:
                continue
            upper = statement.upper()
            if upper.startswith(("SET NAMES", "DROP DATABASE", "CREATE DATABASE", "USE ")):
                continue
            statements.append(statement)
    return statements


def _ensure_app_user(conn: sa.Connection, user: str, password: str, *, host: str) -> None:
    conn.execute(
        sa.text(f"CREATE USER IF NOT EXISTS {_quote_user(user, host)} IDENTIFIED BY :password"),
        {"password": password},
    )


def _validate_database_name(database: str) -> None:
    if not _DATABASE_NAME_PATTERN.fullmatch(database):
        raise ValueError(
            "JARVIS_TEST_MYSQL_DATABASE must contain only letters, numbers, and underscores."
        )


def _quote_identifier(value: str) -> str:
    _validate_database_name(value)
    return f"`{value}`"


def _quote_user(user: str, host: str) -> str:
    return f"'{_escape_sql_string(user)}'@'{_escape_sql_string(host)}'"


def _escape_sql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
