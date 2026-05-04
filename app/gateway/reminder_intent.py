from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ReminderIntent:
    matched: bool
    confidence: float = 0.0
    title: str | None = None
    prompt: str | None = None
    time_text: str | None = None
    timezone: str | None = None
    reason: str = ""
    source: str = "rule"


_REMINDER_MARKERS = ("提醒我", "叫我", "喊我", "到点提醒", "到时提醒", "remind me")
_TIME_HINT_PATTERN = re.compile(
    r"(\d+\s*(?:分钟|分鐘|分|小时|小時|钟头|鐘頭|天)\s*后)|"
    r"((?:今天|明天|明日|后天|今晚|上午|下午|晚上|中午|早上|早晨)?\s*\d{1,2}\s*(?:点|點|:|：)\s*(?:\d{1,2}|半)?)",
    re.IGNORECASE,
)


class RuleReminderIntentDetector:
    def detect(self, text: str) -> ReminderIntent:
        content = (text or "").strip()
        if not content:
            return ReminderIntent(matched=False, reason="empty")
        lowered = content.lower()
        if not any(marker in lowered for marker in _REMINDER_MARKERS):
            return ReminderIntent(matched=False, reason="missing reminder marker")
        time_match = _TIME_HINT_PATTERN.search(content)
        if time_match is None:
            return ReminderIntent(
                matched=True,
                confidence=0.45,
                reason="missing concrete time",
                prompt=_extract_prompt(content),
            )

        time_text = time_match.group(0).strip()
        prompt = _extract_prompt(content.replace(time_text, " ", 1))
        if not prompt:
            prompt = "提醒"
        title = _title_from_prompt(prompt)
        return ReminderIntent(
            matched=True,
            confidence=0.95,
            title=title,
            prompt=prompt,
            time_text=time_text,
            reason="rule reminder marker and time",
        )


def _extract_prompt(text: str) -> str:
    value = text.strip()
    for marker in ("提醒我", "叫我", "喊我", "到点提醒我", "到时提醒我", "remind me"):
        value = value.replace(marker, " ")
    value = re.sub(r"\s+", " ", value).strip(" ，,。.")
    if value.startswith("我"):
        value = value[1:].strip()
    return value or "提醒"


def _title_from_prompt(prompt: str) -> str:
    value = prompt.strip() or "提醒"
    if len(value) > 30:
        value = value[:30].rstrip()
    return f"提醒{value}" if not value.startswith("提醒") else value
