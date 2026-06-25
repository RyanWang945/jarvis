from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal

from app.channels.feishu_renderer import FeishuDelivery, FeishuRenderer

FeishuValidationSeverity = Literal["warning", "error"]

_MAX_TEXT_CONTENT_BYTES = 150 * 1024
_MAX_CARD_CONTENT_BYTES = 28 * 1024
_MAX_CARDKIT_MARKDOWN_BYTES = 8 * 1024
_RAW_AT_PATTERN = re.compile(r"(?<![\w<])@[\w.-]{2,}")


@dataclass(frozen=True)
class FeishuPayloadIssue:
    severity: FeishuValidationSeverity
    code: str
    message: str
    path: str = ""


@dataclass(frozen=True)
class FeishuPayloadValidation:
    ok: bool
    msg_type: str
    content_chars: int
    content_bytes: int
    element_count: int = 0
    markdown_blocks: list[dict[str, Any]] = field(default_factory=list)
    issues: list[FeishuPayloadIssue] = field(default_factory=list)
    content: str = ""

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "warning")


def validate_cardkit_progress_text(
    text: str,
    *,
    title: str = "Jarvis",
    node_id: str = "main",
    runtime: str = "react",
) -> FeishuPayloadValidation:
    """Render final CardKit progress output and run local payload checks.

    This does not call Feishu. It validates the exact JSON shape Jarvis sends in
    the PATCH body for CardKit progress messages.
    """
    renderer = FeishuRenderer(title=title)
    delivery = renderer.render_cardkit_progress_card(
        _completed_progress_snapshot(node_id=node_id, runtime=runtime),
        output_markdown=text,
    )
    return validate_feishu_delivery(delivery)


def validate_feishu_delivery(delivery: FeishuDelivery) -> FeishuPayloadValidation:
    issues: list[FeishuPayloadIssue] = []
    content = delivery.content
    content_bytes = len(content.encode("utf-8"))
    content_chars = len(content)
    markdown_blocks: list[dict[str, Any]] = []
    element_count = 0

    if delivery.msg_type == "text":
        _validate_json_text_payload(content, issues)
        if content_bytes > _MAX_TEXT_CONTENT_BYTES:
            issues.append(
                FeishuPayloadIssue(
                    "error",
                    "text_payload_too_large",
                    f"text payload is {content_bytes} bytes; limit is {_MAX_TEXT_CONTENT_BYTES}",
                )
            )
        return FeishuPayloadValidation(
            ok=not any(issue.severity == "error" for issue in issues),
            msg_type=delivery.msg_type,
            content_chars=content_chars,
            content_bytes=content_bytes,
            issues=issues,
            content=content,
        )

    if delivery.msg_type != "interactive":
        issues.append(
            FeishuPayloadIssue(
                "error",
                "unsupported_msg_type",
                f"unsupported Feishu msg_type: {delivery.msg_type}",
            )
        )
        return FeishuPayloadValidation(
            ok=False,
            msg_type=delivery.msg_type,
            content_chars=content_chars,
            content_bytes=content_bytes,
            issues=issues,
            content=content,
        )

    card = _loads_json_object(content, issues, path="content")
    if card is not None:
        element_count = _count_card_elements(card)
        markdown_blocks = _collect_markdown_blocks(card)
        _validate_card_payload(card, content_bytes, markdown_blocks, issues)

    return FeishuPayloadValidation(
        ok=not any(issue.severity == "error" for issue in issues),
        msg_type=delivery.msg_type,
        content_chars=content_chars,
        content_bytes=content_bytes,
        element_count=element_count,
        markdown_blocks=markdown_blocks,
        issues=issues,
        content=content,
    )


def validation_to_dict(validation: FeishuPayloadValidation) -> dict[str, Any]:
    return {
        "ok": validation.ok,
        "msg_type": validation.msg_type,
        "content_chars": validation.content_chars,
        "content_bytes": validation.content_bytes,
        "element_count": validation.element_count,
        "markdown_blocks": validation.markdown_blocks,
        "issues": [issue.__dict__ for issue in validation.issues],
    }


def _validate_json_text_payload(content: str, issues: list[FeishuPayloadIssue]) -> None:
    payload = _loads_json_object(content, issues, path="content")
    if payload is None:
        return
    text = payload.get("text")
    if not isinstance(text, str):
        issues.append(FeishuPayloadIssue("error", "missing_text", "text payload must contain string field text", "text"))


def _loads_json_object(value: str, issues: list[FeishuPayloadIssue], *, path: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        issues.append(FeishuPayloadIssue("error", "invalid_json", str(exc), path))
        return None
    if not isinstance(payload, dict):
        issues.append(FeishuPayloadIssue("error", "json_not_object", "payload must be a JSON object", path))
        return None
    return payload


def _validate_card_payload(
    card: dict[str, Any],
    content_bytes: int,
    markdown_blocks: list[dict[str, Any]],
    issues: list[FeishuPayloadIssue],
) -> None:
    if content_bytes > _MAX_CARD_CONTENT_BYTES:
        issues.append(
            FeishuPayloadIssue(
                "error",
                "card_payload_too_large",
                f"interactive card payload is {content_bytes} bytes; local limit is {_MAX_CARD_CONTENT_BYTES}",
            )
        )
    if card.get("schema") == "2.0":
        body = card.get("body")
        elements = body.get("elements") if isinstance(body, dict) else None
        if not isinstance(elements, list):
            issues.append(FeishuPayloadIssue("error", "missing_cardkit_body_elements", "CardKit 2.0 body.elements must be a list", "body.elements"))
    else:
        elements = card.get("elements")
        if not isinstance(elements, list):
            issues.append(FeishuPayloadIssue("error", "missing_card_elements", "legacy interactive card elements must be a list", "elements"))

    for block in markdown_blocks:
        content = str(block.get("content") or "")
        byte_len = len(content.encode("utf-8"))
        path = str(block.get("path") or "")
        if byte_len > _MAX_CARDKIT_MARKDOWN_BYTES:
            issues.append(
                FeishuPayloadIssue(
                    "error",
                    "markdown_block_too_large",
                    f"markdown block is {byte_len} bytes; local limit is {_MAX_CARDKIT_MARKDOWN_BYTES}",
                    path,
                )
            )
        if "\t" in content:
            issues.append(
                FeishuPayloadIssue(
                    "warning",
                    "markdown_contains_tab",
                    "markdown contains tab characters; Feishu CardKit rendering can be inconsistent",
                    path,
                )
            )
        if "---" in content:
            issues.append(
                FeishuPayloadIssue(
                    "warning",
                    "markdown_contains_horizontal_rule",
                    "markdown contains horizontal-rule syntax; CardKit markdown may reject or render it differently",
                    path,
                )
            )
        raw_mentions = sorted(set(_RAW_AT_PATTERN.findall(content)))
        if raw_mentions:
            issues.append(
                FeishuPayloadIssue(
                    "warning",
                    "markdown_contains_raw_at_mention",
                    "markdown contains raw @ mention(s): " + ", ".join(raw_mentions[:5]),
                    path,
                )
            )


def _count_card_elements(card: dict[str, Any]) -> int:
    if card.get("schema") == "2.0":
        body = card.get("body")
        elements = body.get("elements") if isinstance(body, dict) else None
    else:
        elements = card.get("elements")
    return len(elements) if isinstance(elements, list) else 0


def _collect_markdown_blocks(card: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            tag = value.get("tag")
            if tag in {"markdown", "lark_md"} and isinstance(value.get("content"), str):
                blocks.append(
                    {
                        "path": path,
                        "tag": tag,
                        "element_id": value.get("element_id"),
                        "chars": len(value["content"]),
                        "bytes": len(value["content"].encode("utf-8")),
                        "content": value["content"],
                    }
                )
            text = value.get("text")
            if isinstance(text, dict) and text.get("tag") in {"lark_md", "markdown"} and isinstance(text.get("content"), str):
                blocks.append(
                    {
                        "path": f"{path}.text",
                        "tag": text.get("tag"),
                        "element_id": value.get("element_id"),
                        "chars": len(text["content"]),
                        "bytes": len(text["content"].encode("utf-8")),
                        "content": text["content"],
                    }
                )
            for key, item in value.items():
                visit(item, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")

    visit(card, "")
    return blocks


def _completed_progress_snapshot(*, node_id: str, runtime: str) -> Any:
    node_label = f"{node_id} ({runtime})" if runtime else node_id
    return SimpleNamespace(
        current_stage="完成",
        current_action="任务已完成，正在返回结果",
        completed_items=["生成执行计划", f"完成 {node_label}", "汇总结果"],
        recent_events=[],
        planned_nodes=[{"id": node_id, "runtime": runtime}],
        completed_node_ids=[node_id],
        node_total=1,
        node_completed=1,
        output_started=True,
        status="completed",
    )
