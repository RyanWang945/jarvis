import json
from pathlib import Path

from app.tools.runtime import execute_tool, get_tool_definition


def test_obsidian_wiki_draft_and_apply_tools(tmp_path: Path) -> None:
    vault = tmp_path / "JarvisWiki"
    draft_tool = get_tool_definition("obsidian_wiki_draft")
    apply_tool = get_tool_definition("obsidian_wiki_apply")

    draft_result = execute_tool(
        draft_tool,
        {
            "vault_path": str(vault),
            "title": "Tool Driven Draft",
            "page_type": "design",
            "content": "This page was created through the tool wrapper.",
            "source_ids": [],
        },
    )
    assert draft_result.ok is True
    draft_payload = json.loads(draft_result.stdout)
    assert draft_payload["draft_id"].startswith("draft_")

    apply_result = execute_tool(
        apply_tool,
        {
            "vault_path": str(vault),
            "draft_id": draft_payload["draft_id"],
        },
    )
    assert apply_result.ok is True
    apply_payload = json.loads(apply_result.stdout)
    assert apply_payload["status"] == "applied"
    assert (vault / "vault" / "projects" / "jarvis" / "designs" / "tool-driven-draft.md").exists()


def test_obsidian_wiki_query_tool_can_read_raw_and_wiki(tmp_path: Path) -> None:
    vault = tmp_path / "JarvisWiki"
    draft_tool = get_tool_definition("obsidian_wiki_draft")
    apply_tool = get_tool_definition("obsidian_wiki_apply")
    query_tool = get_tool_definition("obsidian_wiki_query")

    from app.obsidian_wiki import ObsidianWikiService

    service = ObsidianWikiService(vault)
    source_id = service.create_raw_source(
        source_type="documents",
        title="raw seed",
        content="This raw note contains protocol details.",
        source_ref="doc://protocol",
    )
    draft_result = execute_tool(
        draft_tool,
        {
            "vault_path": str(vault),
            "title": "Protocol Concept",
            "page_type": "concept",
            "content": "This wiki page explains the protocol concept.",
            "source_ids": [source_id],
        },
    )
    draft_payload = json.loads(draft_result.stdout)
    execute_tool(apply_tool, {"vault_path": str(vault), "draft_id": draft_payload["draft_id"]})

    wiki_result = execute_tool(
        query_tool,
        {
            "vault_path": str(vault),
            "query": "protocol concept",
            "query_mode": "wiki_only",
        },
    )
    raw_result = execute_tool(
        query_tool,
        {
            "vault_path": str(vault),
            "query": "protocol details",
            "query_mode": "raw_only",
        },
    )

    wiki_payload = json.loads(wiki_result.stdout)
    raw_payload = json.loads(raw_result.stdout)
    assert any(hit["layer"] == "wiki" for hit in wiki_payload["hits"])
    assert any(hit["layer"] == "raw" for hit in raw_payload["hits"])


def test_obsidian_wiki_maintain_tool_reports_issues(tmp_path: Path) -> None:
    vault = tmp_path / "JarvisWiki"
    maintain_tool = get_tool_definition("obsidian_wiki_maintain")
    from app.obsidian_wiki import ObsidianWikiService

    service = ObsidianWikiService(vault)
    service.init_workspace()
    broken = vault / "vault" / "concepts" / "broken.md"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text(
        "---\n"
        "title: Broken\n"
        "page_type: concept\n"
        "source_ids:\n"
        "  - src_missing\n"
        "---\n"
        "See [[missing-page]].\n",
        encoding="utf-8",
    )

    result = execute_tool(maintain_tool, {"vault_path": str(vault)})

    assert result.ok is True
    payload = json.loads(result.stdout)
    codes = {issue["code"] for issue in payload["issues"]}
    assert "missing_source_ids" in codes
    assert "dead_link" in codes


def test_obsidian_wiki_draft_tool_can_compile_from_source(tmp_path: Path) -> None:
    vault = tmp_path / "JarvisWiki"
    draft_tool = get_tool_definition("obsidian_wiki_draft")
    apply_tool = get_tool_definition("obsidian_wiki_apply")

    from app.obsidian_wiki import ObsidianWikiService

    service = ObsidianWikiService(vault)
    source_id = service.create_raw_source(
        source_type="documents",
        title="Compiled Tool Page",
        content="# Compiled Tool Page\n\nCompiler intro.\n\n## Design\n\nTool generated section.\n",
        source_ref="doc://compiled-tool",
    )

    draft_result = execute_tool(
        draft_tool,
        {
            "vault_path": str(vault),
            "title": "Compiled Tool Page",
            "page_type": "design",
            "source_ids": [source_id],
            "target_hint": "projects/jarvis/designs/compiled-tool-page.md",
        },
    )
    assert draft_result.ok is True
    draft_payload = json.loads(draft_result.stdout)

    apply_result = execute_tool(
        apply_tool,
        {
            "vault_path": str(vault),
            "draft_id": draft_payload["draft_id"],
        },
    )
    assert apply_result.ok is True
    page_text = (vault / "vault" / "projects" / "jarvis" / "designs" / "compiled-tool-page.md").read_text(encoding="utf-8")
    assert "# Summary" in page_text
    assert "# Design" in page_text
