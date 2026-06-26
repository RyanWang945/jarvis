from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy import create_engine

from app.config import get_settings


DEFAULT_OUTPUT = Path("tests/fixtures/agent_eval/feishu_real.jsonl")


@dataclass(frozen=True)
class ExportOptions:
    id_prefix: str = "feishu_conv"
    category: str = "feishu_real"
    min_user_messages: int = 1
    include_commands: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export persisted Feishu conversations into the agent_eval JSONL format."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--database-url", default=None, help="Override the MySQL SQLAlchemy URL.")
    parser.add_argument("--conversation-id", type=int, action="append", default=[])
    parser.add_argument("--chat-id", action="append", default=[])
    parser.add_argument("--since", default=None, help="Conversation last_message_at lower bound, e.g. 2026-06-01.")
    parser.add_argument("--until", default=None, help="Conversation last_message_at upper bound, e.g. 2026-07-01.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--id-prefix", default="feishu_conv")
    parser.add_argument("--category", default="feishu_real")
    parser.add_argument("--min-user-messages", type=int, default=1)
    parser.add_argument(
        "--include-commands",
        action="store_true",
        help="Keep slash-command messages such as /clear in exported cases.",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to the output file instead of replacing it.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = fetch_feishu_message_rows(
        database_url=args.database_url,
        conversation_ids=args.conversation_id,
        chat_ids=args.chat_id,
        since=args.since,
        until=args.until,
        limit=args.limit,
    )
    cases = rows_to_agent_eval_cases(
        rows,
        ExportOptions(
            id_prefix=args.id_prefix,
            category=args.category,
            min_user_messages=args.min_user_messages,
            include_commands=args.include_commands,
        ),
    )
    write_jsonl(args.output, cases, append=args.append)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "case_count": len(cases),
                "message_count": sum(len(case["messages"]) for case in cases),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def fetch_feishu_message_rows(
    *,
    database_url: str | None = None,
    conversation_ids: list[int] | None = None,
    chat_ids: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    engine = create_engine(database_url or _mysql_url(), pool_pre_ping=True, pool_recycle=3600)
    conversation_ids = conversation_ids or []
    chat_ids = chat_ids or []
    limit = max(1, limit)

    where = ["platform = 'feishu'"]
    params: dict[str, Any] = {"limit": limit}
    if conversation_ids:
        where.append("id IN :conversation_ids")
        params["conversation_ids"] = conversation_ids
    if chat_ids:
        where.append("external_chat_id IN :chat_ids")
        params["chat_ids"] = chat_ids
    if since:
        where.append("COALESCE(last_message_at, updated_at, created_at) >= :since")
        params["since"] = since
    if until:
        where.append("COALESCE(last_message_at, updated_at, created_at) < :until")
        params["until"] = until

    conv_sql = sa.text(
        "SELECT id FROM conversations "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY COALESCE(last_message_at, updated_at, created_at) DESC, id DESC "
        "LIMIT :limit"
    )
    if conversation_ids:
        conv_sql = conv_sql.bindparams(sa.bindparam("conversation_ids", expanding=True))
    if chat_ids:
        conv_sql = conv_sql.bindparams(sa.bindparam("chat_ids", expanding=True))

    with engine.begin() as conn:
        selected = [row["id"] for row in conn.execute(conv_sql, params).mappings().all()]
        if not selected:
            return []
        msg_sql = sa.text(
            "SELECT "
            "c.id AS conversation_id, c.external_chat_id, c.chat_type, c.title, "
            "m.id AS message_id, m.turn_id, m.sender_type, m.role, m.content, "
            "m.content_type, m.external_message_id, m.reply_to_message_id, "
            "m.raw_payload, m.created_at "
            "FROM conversations c "
            "JOIN messages m ON m.conversation_id = c.id "
            "WHERE c.id IN :conversation_ids "
            "ORDER BY c.id ASC, m.created_at ASC, m.id ASC"
        ).bindparams(sa.bindparam("conversation_ids", expanding=True))
        return [dict(row) for row in conn.execute(msg_sql, {"conversation_ids": selected}).mappings().all()]


def rows_to_agent_eval_cases(rows: Iterable[dict[str, Any]], options: ExportOptions | None = None) -> list[dict[str, Any]]:
    options = options or ExportOptions()
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["conversation_id"])].append(row)

    cases: list[dict[str, Any]] = []
    for conversation_id in sorted(grouped):
        conv_rows = grouped[conversation_id]
        user_messages = [_message_payload(row, options=options) for row in conv_rows]
        user_messages = [message for message in user_messages if message is not None]
        if len(user_messages) < options.min_user_messages:
            continue

        first = conv_rows[0]
        external_chat_id = str(first.get("external_chat_id") or "")
        case = {
            "id": f"{_slug(options.id_prefix)}_{conversation_id}",
            "category": options.category,
            "description": f"Imported from Feishu conversation {conversation_id}.",
            "messages": user_messages,
            "expected_tools": [],
            "forbidden_tools": [],
            "required_status": "completed",
            "success_criteria": [
                "Replay real Feishu user messages without crashing.",
                "Final turn should complete successfully.",
            ],
            "metadata": {
                "source": "feishu_conversation_export",
                "conversation_id": conversation_id,
                "external_chat_id": external_chat_id,
                "chat_type": first.get("chat_type"),
                "exported_at": datetime.now().astimezone().isoformat(),
            },
        }
        cases.append(case)
    return cases


def write_jsonl(path: Path, cases: list[dict[str, Any]], *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "\n")


def _message_payload(row: dict[str, Any], *, options: ExportOptions) -> dict[str, Any] | None:
    if row.get("role") != "user":
        return None
    content = str(row.get("content") or "").strip()
    if not content:
        return None
    if not options.include_commands and content.startswith("/"):
        return None

    message: dict[str, Any] = {
        "content": content,
        "content_type": row.get("content_type") or "text",
    }
    external_message_id = row.get("external_message_id")
    if external_message_id:
        message["external_message_id"] = str(external_message_id)
    return message


def _mysql_url() -> str:
    settings = get_settings()
    return (
        f"mysql+pymysql://{settings.mysql_user}:{settings.mysql_password}"
        f"@{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}"
        f"?charset=utf8mb4"
    )


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip())
    normalized = normalized.strip("_")
    return normalized or "feishu_conv"


if __name__ == "__main__":
    raise SystemExit(main())
