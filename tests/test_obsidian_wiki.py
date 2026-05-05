from pathlib import Path

from app.obsidian_wiki import ObsidianWikiService


DESIGN_DOC = Path(r"E:\pythonProject\jarvis\docs\design\2026-5\【2026-5-2】 obsidian_wiki.md")


def test_init_workspace_creates_expected_directories(tmp_path: Path) -> None:
    service = ObsidianWikiService(tmp_path / "JarvisWiki")

    service.init_workspace()

    assert (service.vault_path / "index.md").exists()
    assert (service.system_path / "drafts").is_dir()
    assert (service.system_path / "raw" / "conversations").is_dir()
    assert (service.system_path / "schema" / "page-types.md").exists()
    assert not (service.vault_path / "drafts").exists()
    assert not (service.vault_path / "raw").exists()
    assert not (service.vault_path / "schema").exists()


def test_draft_and_apply_create_page(tmp_path: Path) -> None:
    service = ObsidianWikiService(tmp_path / "JarvisWiki")
    source_id = service.create_raw_source(
        source_type="documents",
        title="design source",
        content="Jarvis should use explicit draft then apply.",
        source_ref="doc://design",
    )

    draft = service.draft(
        title="Draft Apply Flow",
        page_type="design",
        content="This page records the explicit draft to apply flow.",
        source_ids=[source_id],
    )
    result = service.apply(draft.draft_id)

    assert result.status == "applied"
    assert result.page_path is not None
    page_text = result.page_path.read_text(encoding="utf-8")
    assert "page_type: design" in page_text
    assert source_id in page_text
    assert "explicit draft to apply flow" in page_text


def test_apply_detects_conflict_when_target_changed(tmp_path: Path) -> None:
    service = ObsidianWikiService(tmp_path / "JarvisWiki")
    draft = service.draft(
        title="Conflict Page",
        page_type="concept",
        content="Initial concept body.",
        source_ids=[],
    )
    target = service.vault_path / draft.target_page
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntitle: Conflict Page\npage_type: concept\nsource_ids: []\n---\nDifferent body.\n",
        encoding="utf-8",
    )

    result = service.apply(draft.draft_id)

    assert result.status == "conflict"
    assert result.conflict_reason == "target page changed after draft creation"


def test_query_supports_wiki_then_raw(tmp_path: Path) -> None:
    service = ObsidianWikiService(tmp_path / "JarvisWiki")
    source_id = service.create_raw_source(
        source_type="documents",
        title="raw note",
        content="Hidden protocol lives in raw only.",
        source_ref="doc://raw",
    )
    draft = service.draft(
        title="Visible Concept",
        page_type="concept",
        content="Jarvis uses visible concept pages for retrieval.",
        source_ids=[source_id],
    )
    service.apply(draft.draft_id)

    wiki_hits = service.query("visible concept", query_mode="wiki_only")
    raw_hits = service.query("hidden protocol", query_mode="raw_only")
    mixed_hits = service.query("hidden protocol", query_mode="wiki_then_raw")

    assert any(hit.layer == "wiki" for hit in wiki_hits)
    assert any(hit.layer == "raw" for hit in raw_hits)
    assert any(hit.layer == "raw" for hit in mixed_hits)


def test_maintain_reports_missing_source_and_dead_link(tmp_path: Path) -> None:
    service = ObsidianWikiService(tmp_path / "JarvisWiki")
    service.init_workspace()
    broken_page = service.vault_path / "concepts" / "broken.md"
    broken_page.parent.mkdir(parents=True, exist_ok=True)
    broken_page.write_text(
        "---\n"
        "title: Broken\n"
        "page_type: concept\n"
        "source_ids:\n"
        "  - src_missing\n"
        "---\n"
        "See [[missing-page]].\n",
        encoding="utf-8",
    )

    result = service.maintain()
    codes = {issue.code for issue in result.issues if issue.path == broken_page}

    assert "missing_source_ids" in codes
    assert "dead_link" in codes


def test_design_doc_can_be_ingested_drafted_applied_and_queried(tmp_path: Path) -> None:
    service = ObsidianWikiService(tmp_path / "JarvisWiki")
    content = DESIGN_DOC.read_text(encoding="utf-8")

    source_id = service.create_raw_source(
        source_type="documents",
        title="Jarvis obsidian_wiki 设计",
        content=content,
        source_ref=str(DESIGN_DOC),
    )
    draft = service.draft(
        title="Obsidian Wiki Design",
        page_type="design",
        content=(
            "This page summarizes the Jarvis obsidian_wiki design, including explicit draft to apply flow, "
            "query behavior, source tracing, and the v1 scope."
        ),
        source_ids=[source_id],
        target_hint="projects/jarvis/designs/obsidian-wiki-design.md",
    )
    apply_result = service.apply(draft.draft_id)
    query_hits = service.query("explicit draft to apply flow", query_mode="wiki_then_raw")

    assert apply_result.status == "applied"
    assert apply_result.page_path == service.vault_path / "projects" / "jarvis" / "designs" / "obsidian-wiki-design.md"
    page_text = apply_result.page_path.read_text(encoding="utf-8")
    assert source_id in page_text
    assert "source_mode: generated" in page_text
    assert any(hit.layer == "wiki" for hit in query_hits)


def test_draft_can_compile_structured_page_from_source(tmp_path: Path) -> None:
    service = ObsidianWikiService(tmp_path / "JarvisWiki")
    source_id = service.create_raw_source(
        source_type="documents",
        title="Structured Design",
        content=(
            "# Structured Design\n\n"
            "This design explains how the wiki compiler should preserve useful structure.\n\n"
            "## Background\n\n"
            "Background section body.\n\n"
            "## Design\n\n"
            "Design section body.\n\n"
            "## Next Steps\n\n"
            "Next step body.\n"
        ),
        source_ref="doc://structured",
    )

    draft = service.draft(
        title="Structured Design",
        page_type="design",
        content="",
        source_ids=[source_id],
        target_hint="projects/jarvis/designs/structured-design.md",
    )
    result = service.apply(draft.draft_id)

    assert result.status == "applied"
    page_text = result.page_path.read_text(encoding="utf-8")
    assert "# Summary" in page_text
    assert "# Related" in page_text
    assert "# Background" in page_text
    assert "# Design" in page_text
    assert "This design explains how the wiki compiler should preserve useful structure." in page_text
    assert "Source Ref: `doc://structured`" in page_text


def test_apply_refreshes_root_index_without_all_to_all_related_links(tmp_path: Path) -> None:
    service = ObsidianWikiService(tmp_path / "JarvisWiki")
    first_source = service.create_raw_source(
        source_type="documents",
        title="First Design",
        content="# First Design\n\nFirst intro.\n\n## Design\n\nFirst body.\n",
        source_ref="doc://first",
    )
    second_source = service.create_raw_source(
        source_type="documents",
        title="Second Design",
        content="# Second Design\n\nSecond intro.\n\n## Design\n\nSecond body.\n",
        source_ref="doc://second",
    )

    first_draft = service.draft(
        title="First Design",
        page_type="design",
        content="",
        source_ids=[first_source],
        target_hint="projects/jarvis/designs/first-design.md",
    )
    first_result = service.apply(first_draft.draft_id)
    second_draft = service.draft(
        title="Second Design",
        page_type="design",
        content="",
        source_ids=[second_source],
        target_hint="projects/jarvis/designs/second-design.md",
    )
    second_result = service.apply(second_draft.draft_id)

    index_text = (service.vault_path / "index.md").read_text(encoding="utf-8")
    first_page = first_result.page_path.read_text(encoding="utf-8")
    second_page = second_result.page_path.read_text(encoding="utf-8")

    assert "[[projects/jarvis/designs/first-design]]" in index_text
    assert "[[projects/jarvis/designs/second-design]]" in index_text
    assert "[[index]]" in first_page
    assert "[[index]]" in second_page
    assert "[[projects/jarvis/designs/second-design]]" not in first_page
    assert "[[projects/jarvis/designs/first-design]]" not in second_page
