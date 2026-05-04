from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app.api.agent import get_agent_runtime, get_conversation_store
from app.api.schemas import (
    ConversationMessageCreateRequest,
    MessageCreateRequest,
    SenderInput,
)
from app.gateway import InboundEvent, get_gateway_service


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    output = args.func(args)
    _print_json(output)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis-cli")
    subcommands = parser.add_subparsers(dest="command", required=True)

    chat = subcommands.add_parser("chat", help="Send a message into a conversation.")
    chat.add_argument("message")
    chat.add_argument("--conversation-id", type=int)
    chat.add_argument("--external-chat-id", default="cli")
    chat.add_argument("--user-id", default="cli-user")
    chat.set_defaults(func=_chat)

    run = subcommands.add_parser("run", help="Alias for chat.")
    run.add_argument("message")
    run.add_argument("--conversation-id", type=int)
    run.add_argument("--external-chat-id", default="cli")
    run.add_argument("--user-id", default="cli-user")
    run.set_defaults(func=_chat)

    status = subcommands.add_parser("status", help="Inspect one turn.")
    status.add_argument("turn_id", type=int)
    status.set_defaults(func=_status)

    return parser


def _chat(args: argparse.Namespace) -> dict[str, Any]:
    if args.conversation_id is None:
        gateway = get_gateway_service()
        gateway_result = gateway.handle_inbound_event(
            InboundEvent(
                platform="cli",
                external_chat_id=args.external_chat_id,
                external_message_id=None,
                chat_type="cli",
                sender_id=args.user_id,
                sender_name=None,
                text=args.message,
            )
        )
        if not gateway_result.should_run_agent:
            return {
                "conversation_id": gateway_result.conversation_id,
                "message_id": gateway_result.message_id,
                "turn_id": gateway_result.turn_id,
                "status": gateway_result.status,
                "reply": gateway_result.immediate_reply,
            }
        result = None
        if gateway_result.turn_id is not None:
            result = get_agent_runtime().run_turn(gateway_result.turn_id)
        return {
            "conversation_id": gateway_result.conversation_id,
            "message_id": gateway_result.message_id,
            "turn_id": gateway_result.turn_id,
            "status": result.status if result else gateway_result.status,
            "reply": result.reply if result else None,
        }

    store = get_conversation_store()
    if args.conversation_id is not None:
        ingest = store.ingest_conversation_message(
            args.conversation_id,
            ConversationMessageCreateRequest(
                sender=SenderInput(platform_user_id=args.user_id),
                content=args.message,
            ),
        )
    else:
        ingest = store.ingest_message(
            MessageCreateRequest(
                platform="cli",
                external_chat_id=args.external_chat_id,
                chat_type="cli",
                sender=SenderInput(platform_user_id=args.user_id),
                content=args.message,
            )
        )

    result = None
    if ingest.turn_id is not None:
        result = get_agent_runtime().run_turn(ingest.turn_id)

    return {
        "conversation_id": ingest.conversation_id,
        "message_id": ingest.message_id,
        "turn_id": ingest.turn_id,
        "status": result.status if result else ingest.status,
        "reply": result.reply if result else None,
    }


def _status(args: argparse.Namespace) -> dict[str, Any]:
    turn = get_conversation_store().get_turn(args.turn_id)
    if turn is None:
        raise SystemExit(f"Turn not found: {args.turn_id}")
    return {
        "turn_id": turn.id,
        "conversation_id": turn.conversation_id,
        "trigger_message_id": turn.trigger_message_id,
        "status": turn.status,
        "turn_type": turn.turn_type,
        "metadata": turn.metadata,
    }


def _print_json(output: Any) -> None:
    text = json.dumps(output, ensure_ascii=False, indent=2, default=str)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")
