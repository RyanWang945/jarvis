from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import yaml

from app.obsidian_wiki.models import ApplyResult, DraftResult, MaintainIssue, MaintainResult, QueryHit

PAGE_TYPES = {"index", "design", "decision", "concept", "playbook"}
REQUIRED_DIRS = (
    "vault/inbox",
    "vault/projects",
    "vault/concepts",
    "vault/tools",
    "vault/playbooks",
    "system/schema",
    "system/raw/conversations",
    "system/raw/documents",
    "system/raw/web",
    "system/raw/repos",
    "system/raw/obsidian-notes",
    "system/drafts",
    "system/templates",
    "system/logs",
)


class ObsidianWikiService:
    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = workspace_path

    @property
    def vault_path(self) -> Path:
        return self.workspace_path / "vault"

    @property
    def system_path(self) -> Path:
        return self.workspace_path / "system"

    def init_workspace(self) -> None:
        for rel in REQUIRED_DIRS:
            (self.workspace_path / rel).mkdir(parents=True, exist_ok=True)
        self._write_if_missing(
            self.workspace_path / "README.md",
            "# Obsidian Wiki Workspace\n\nOpen `vault/` as the Obsidian vault. `system/` stores raw sources, drafts, schema, and logs.\n",
        )
        self._write_if_missing(
            self.vault_path / "index.md",
            _render_frontmatter({"title": "Jarvis Wiki", "page_type": "index", "status": "active", "source_ids": [], "source_mode": "manual"})
            + "\n# Wiki Index\n",
        )
        self._write_if_missing(self.system_path / "schema" / "page-types.md", "\n".join(sorted(PAGE_TYPES)) + "\n")
        self._write_if_missing(self.system_path / "schema" / "naming.md", "# Naming\n")
        self._write_if_missing(self.system_path / "schema" / "writing-rules.md", "# Writing Rules\n")
        self._write_if_missing(self.system_path / "schema" / "wiki-schema.md", "# Wiki Schema\n")

    def create_raw_source(
        self,
        *,
        source_type: str,
        title: str,
        content: str,
        source_ref: str,
    ) -> str:
        self.init_workspace()
        directory = self.system_path / "raw" / source_type
        directory.mkdir(parents=True, exist_ok=True)
        source_id = f"src_{uuid4().hex[:12]}"
        payload = {
            "source_id": source_id,
            "source_type": source_type,
            "title": title,
            "source_ref": source_ref,
            "content_hash": _content_hash(content),
        }
        body = _render_frontmatter(payload) + "\n" + content.strip() + "\n"
        (directory / f"{source_id}.md").write_text(body, encoding="utf-8")
        return source_id

    def draft(
        self,
        *,
        title: str,
        page_type: str,
        content: str,
        source_ids: list[str],
        target_hint: str | None = None,
    ) -> DraftResult:
        self.init_workspace()
        if page_type not in PAGE_TYPES:
            raise ValueError(f"invalid page_type: {page_type}")
        draft_id = f"draft_{uuid4().hex[:12]}"
        target_page = target_hint or self._default_target_page(page_type, title)
        target_path = self.vault_path / target_page
        base_hash = _content_hash(target_path.read_text(encoding="utf-8")) if target_path.exists() else None
        body_content = content.strip() or self._compile_page_content(
            title=title,
            page_type=page_type,
            source_ids=source_ids,
            target_page=target_page,
        )
        frontmatter = {
            "draft_id": draft_id,
            "title": title,
            "page_type": page_type,
            "source_ids": source_ids,
            "target_page": target_page,
            "base_content_hash": base_hash,
        }
        draft_path = self.system_path / "drafts" / f"{draft_id}.md"
        draft_path.write_text(_render_frontmatter(frontmatter) + "\n" + body_content + "\n", encoding="utf-8")
        return DraftResult(
            draft_id=draft_id,
            path=draft_path,
            page_type=page_type,
            title=title,
            target_page=target_page,
            source_ids=list(source_ids),
        )

    def apply(self, draft_id: str, *, target_page: str | None = None) -> ApplyResult:
        self.init_workspace()
        draft_path = self.system_path / "drafts" / f"{draft_id}.md"
        if not draft_path.exists():
            raise ValueError(f"unknown draft: {draft_id}")
        frontmatter, body = _parse_markdown(draft_path.read_text(encoding="utf-8"))
        page_path = self.vault_path / str(target_page or frontmatter["target_page"])
        if page_path.exists():
            current_hash = _content_hash(page_path.read_text(encoding="utf-8"))
            base_hash = frontmatter.get("base_content_hash")
            if base_hash is None or base_hash != current_hash:
                return ApplyResult(
                    status="conflict",
                    page_path=page_path,
                    conflict_reason="target page changed after draft creation",
                )
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_frontmatter = {
            "title": frontmatter["title"],
            "page_type": frontmatter["page_type"],
            "status": "active",
            "source_ids": frontmatter.get("source_ids", []),
            "source_mode": "generated",
        }
        page_path.write_text(_render_frontmatter(page_frontmatter) + "\n" + body.strip() + "\n", encoding="utf-8")
        self._refresh_related_links(page_path)
        self._refresh_root_index()
        return ApplyResult(status="applied", page_path=page_path)

    def query(self, query: str, *, query_mode: str = "wiki_then_raw", limit: int = 5) -> list[QueryHit]:
        self.init_workspace()
        results: list[QueryHit] = []
        if query_mode not in {"wiki_only", "wiki_then_raw", "raw_only"}:
            raise ValueError(f"invalid query_mode: {query_mode}")
        if query_mode != "raw_only":
            results.extend(self._search_markdown_tree(self.vault_path, query, layer="wiki"))
        if query_mode in {"wiki_then_raw", "raw_only"} and len(results) < limit:
            results.extend(self._search_markdown_tree(self.system_path / "raw", query, layer="raw"))
        return results[:limit]

    def maintain(self) -> MaintainResult:
        self.init_workspace()
        issues: list[MaintainIssue] = []
        for path in self.vault_path.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            frontmatter, _body = _parse_markdown(text)
            if not frontmatter:
                issues.append(MaintainIssue(path=path, code="missing_frontmatter", message="missing frontmatter"))
                continue
            page_type = frontmatter.get("page_type")
            if page_type is None:
                # allow root index without page_type
                if path != self.vault_path / "index.md":
                    issues.append(MaintainIssue(path=path, code="missing_page_type", message="missing page_type"))
            elif page_type not in PAGE_TYPES:
                issues.append(MaintainIssue(path=path, code="invalid_page_type", message=f"invalid page_type: {page_type}"))
            if "source_ids" in frontmatter:
                missing = [source_id for source_id in frontmatter.get("source_ids", []) if not self._source_exists(source_id)]
                if missing:
                    issues.append(MaintainIssue(path=path, code="missing_source_ids", message="missing source_ids: " + ", ".join(missing)))
            for link in _extract_wiki_links(text):
                if not (self.vault_path / f"{link}.md").exists():
                    issues.append(MaintainIssue(path=path, code="dead_link", message=f"dead link: {link}"))
        return MaintainResult(issues=issues)

    def _search_markdown_tree(self, root: Path, query: str, *, layer: str) -> list[QueryHit]:
        results: list[QueryHit] = []
        needle = query.casefold()
        for path in root.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            if needle not in text.casefold():
                continue
            frontmatter, body = _parse_markdown(text)
            title = str(frontmatter.get("title") or path.stem)
            snippet = _snippet(body or text, query)
            results.append(QueryHit(path=path, title=title, snippet=snippet, layer=layer))
        results.sort(key=lambda item: item.path.as_posix())
        return results

    def _source_exists(self, source_id: str) -> bool:
        return any(path.name == f"{source_id}.md" for path in self.system_path.joinpath("raw").rglob("*.md"))

    def _default_target_page(self, page_type: str, title: str) -> str:
        slug = _slugify(title)
        if page_type == "concept":
            return f"concepts/{slug}.md"
        if page_type == "playbook":
            return f"playbooks/{slug}.md"
        if page_type == "index":
            return f"projects/{slug}/index.md"
        return f"projects/jarvis/{page_type}s/{slug}.md"

    def _write_if_missing(self, path: Path, content: str) -> None:
        if not path.exists():
            path.write_text(content, encoding="utf-8")

    def _compile_page_content(self, *, title: str, page_type: str, source_ids: list[str], target_page: str) -> str:
        if not source_ids:
            raise ValueError("draft requires content or at least one source_id")
        primary = self._load_source(source_ids[0])
        related_links = self._related_links(target_page)
        summary = _first_paragraph(primary.body)
        sections = _select_sections(primary.body, page_type=page_type)
        lines: list[str] = []
        lines.append("# Summary")
        lines.append("")
        lines.append(summary or f"{title} compiled from source `{primary.source_id}`.")
        lines.append("")
        if related_links:
            lines.append("# Related")
            lines.append("")
            for link in related_links:
                lines.append(f"- [[{link}]]")
            lines.append("")
        if sections:
            lines.extend(sections)
            lines.append("")
        lines.append("# Source")
        lines.append("")
        lines.append(f"- Source ID: `{primary.source_id}`")
        lines.append(f"- Source Ref: `{primary.source_ref}`")
        return "\n".join(lines).strip()

    def _load_source(self, source_id: str) -> "RawSource":
        for path in self.system_path.joinpath("raw").rglob(f"{source_id}.md"):
            frontmatter, body = _parse_markdown(path.read_text(encoding="utf-8"))
            return RawSource(
                source_id=source_id,
                path=path,
                title=str(frontmatter.get("title") or path.stem),
                source_ref=str(frontmatter.get("source_ref") or ""),
                body=body.strip(),
            )
        raise ValueError(f"unknown source_id: {source_id}")

    def _related_links(self, target_page: str) -> list[str]:
        target_path = self.vault_path / target_page
        return self._context_links_for_page(target_path)

    def _refresh_related_links(self, page_path: Path) -> None:
        text = page_path.read_text(encoding="utf-8")
        frontmatter, body = _parse_markdown(text)
        current_link = page_path.relative_to(self.vault_path).with_suffix("").as_posix()
        links = [link for link in self._context_links_for_page(page_path) if link != current_link]
        updated_body = _replace_related_section(body, links)
        page_path.write_text(_render_frontmatter(frontmatter) + "\n" + updated_body.strip() + "\n", encoding="utf-8")

    def _context_links_for_page(self, page_path: Path) -> list[str]:
        links = ["index"]
        collection_index = page_path.parent.with_suffix(".md")
        if collection_index.exists() and collection_index != page_path:
            links.append(collection_index.relative_to(self.vault_path).with_suffix("").as_posix())
        seen: list[str] = []
        for link in links:
            if link not in seen:
                seen.append(link)
        return seen

    def _refresh_root_index(self) -> None:
        sections: list[str] = ["# Jarvis Wiki", "", "这个 vault 用来沉淀 Jarvis 的长期知识页。当前主入口如下：", ""]
        design_pages = sorted(self.vault_path.glob("projects/*/designs/*.md"))
        if design_pages:
            sections.extend(["## 设计", ""])
            for path in design_pages:
                sections.append(f"- [[{path.relative_to(self.vault_path).with_suffix('').as_posix()}]]")
            sections.append("")
        concept_pages = sorted(self.vault_path.glob("concepts/*.md"))
        if concept_pages:
            sections.extend(["## 概念", ""])
            for path in concept_pages:
                sections.append(f"- [[{path.relative_to(self.vault_path).with_suffix('').as_posix()}]]")
            sections.append("")
        playbook_pages = sorted(self.vault_path.glob("playbooks/*.md"))
        if playbook_pages:
            sections.extend(["## Playbooks", ""])
            for path in playbook_pages:
                sections.append(f"- [[{path.relative_to(self.vault_path).with_suffix('').as_posix()}]]")
            sections.append("")
        sections.extend(
            [
                "## 说明",
                "",
                "- `projects/` 放项目级设计与决策",
                "- `concepts/` 放长期稳定概念",
                "- `playbooks/` 放操作手册",
                "- `inbox/` 放暂未归类但值得保留的页面",
            ]
        )
        frontmatter = {"title": "Jarvis Wiki", "page_type": "index", "status": "active", "source_ids": [], "source_mode": "generated"}
        (self.vault_path / "index.md").write_text(_render_frontmatter(frontmatter) + "\n" + "\n".join(sections).strip() + "\n", encoding="utf-8")


@dataclass(frozen=True)
class RawSource:
    source_id: str
    path: Path
    title: str
    source_ref: str
    body: str


def _render_frontmatter(data: dict) -> str:
    return "---\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip() + "\n---"


def _parse_markdown(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = yaml.safe_load(text[4:end]) or {}
    body = text[end + 4 :].lstrip("\n")
    return raw, body


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slugify(value: str) -> str:
    lowered = value.strip().casefold()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    return lowered.strip("-") or "untitled"


def _snippet(text: str, query: str, *, width: int = 120) -> str:
    index = text.casefold().find(query.casefold())
    if index == -1:
        return text[:width].strip()
    start = max(0, index - 30)
    end = min(len(text), index + len(query) + 60)
    return text[start:end].strip().replace("\n", " ")


def _extract_wiki_links(text: str) -> list[str]:
    return re.findall(r"\[\[([^\]]+)\]\]", text)


def _replace_related_section(body: str, links: list[str]) -> str:
    lines = body.splitlines()
    start = None
    end = None
    for index, line in enumerate(lines):
        if line.strip() == "# Related":
            start = index
            end = len(lines)
            for probe in range(index + 1, len(lines)):
                if lines[probe].startswith("# ") and lines[probe].strip() != "# Related":
                    end = probe
                    break
            break
    related_lines = ["# Related", ""]
    for link in links:
        related_lines.append(f"- [[{link}]]")
    related_lines.append("")
    if start is None:
        insert_at = 0
        for index, line in enumerate(lines):
            if line.strip() == "# Summary":
                insert_at = index + 1
                while insert_at < len(lines) and not lines[insert_at].startswith("# "):
                    insert_at += 1
                break
        new_lines = lines[:insert_at] + [""] + related_lines + lines[insert_at:]
        return "\n".join(_trim_blank_edges(new_lines))
    new_lines = lines[:start] + related_lines + lines[end:]
    return "\n".join(_trim_blank_edges(new_lines))


def _first_paragraph(text: str) -> str:
    cleaned = re.sub(r"^# .+\n+", "", text.strip(), count=1)
    parts = re.split(r"\n\s*\n", cleaned)
    for part in parts:
        candidate = part.strip()
        if not candidate or candidate == "---" or candidate.startswith("#") or candidate.startswith("|"):
            continue
        if not re.search(r"[\w\u4e00-\u9fff]", candidate):
            continue
        return " ".join(line.strip() for line in candidate.splitlines())
    return ""


def _select_sections(text: str, *, page_type: str, max_sections: int = 4) -> list[str]:
    blocks = _markdown_heading_blocks(text)
    if not blocks:
        excerpt = text.strip()
        if not excerpt:
            return []
        return ["# Notes", "", excerpt[:2500].rstrip()]
    selected: list[str] = []
    for heading, body in blocks:
        normalized = heading.strip("# ").strip()
        if not normalized:
            continue
        selected.append(f"# {normalized}")
        selected.append("")
        selected.append(body.strip())
        selected.append("")
        if len(selected) >= max_sections * 4:
            break
    while selected and selected[-1] == "":
        selected.pop()
    return selected


def _markdown_heading_blocks(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    blocks: list[tuple[str, str]] = []
    current_heading: str | None = None
    current_body: list[str] = []
    for line in lines:
        if re.match(r"^##+\s+", line):
            if current_heading is not None and current_body:
                blocks.append((current_heading, "\n".join(current_body).strip()))
            current_heading = line
            current_body = []
            continue
        if current_heading is not None:
            current_body.append(line)
    if current_heading is not None and current_body:
        blocks.append((current_heading, "\n".join(current_body).strip()))
    return blocks


def _trim_blank_edges(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines
