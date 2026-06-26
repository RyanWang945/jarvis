from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.skills.manifest import SkillManifest


@dataclass(frozen=True)
class Skill:
    skill_id: str
    description: str
    effective_description: str
    path: Path
    manifest: SkillManifest
    content_path: Path | None = None

    @property
    def display_name(self) -> str:
        return self.manifest.name or self.skill_id

    def load_body(self) -> str:
        if self.content_path is None or not self.content_path.exists():
            return ""
        return _strip_frontmatter(self.content_path.read_text(encoding="utf-8"))


def _strip_frontmatter(content: str) -> str:
    if not (content.startswith("---\n") or content.startswith("---\r\n")):
        return content
    end = content.find("\n---", 4)
    if end == -1:
        return content
    body_start = end + len("\n---")
    while body_start < len(content) and content[body_start] in {"\r", "\n"}:
        body_start += 1
    return content[body_start:]
