from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.skills.manifest import SkillManifest


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    manifest: SkillManifest
    content_path: Path | None = None

    def load_body(self) -> str:
        if self.content_path is None or not self.content_path.exists():
            return ""
        return self.content_path.read_text(encoding="utf-8")
