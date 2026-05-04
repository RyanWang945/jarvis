---
name: web-search-guide
description: Use when answering questions that need current or external web evidence.
when_to_use: User asks for latest information, recent news, current facts, web lookup, source-backed verification, or external references.
tools:
  - tavily_search
tags:
  - search
  - web
  - research
---

# Web Search Guide

Use `tavily_search` when the user asks for current, recent, time-sensitive, or externally verifiable facts.

Tavily can also search indexed X/Twitter pages or web coverage about X/Twitter activity when the user wants a general web/news view.

Prefer `x_search` for direct X/Twitter post search, latest tweets, named account posts, or social sentiment on X.

Do not use web search for local repository files, conversation memory, or knowledge already available in Jarvis context. Use the appropriate local tool instead.

Parameter guidance:

- Use `search_depth: basic` for quick facts and narrow questions.
- Use `search_depth: advanced` for research, comparisons, or questions that need multiple perspectives.
- Use `topic: news` for recent news and time-sensitive developments.
- Use `include_domains` when the user asks for specific sources or official documentation.
- Use `exclude_domains` only to remove clearly unwanted domains.
- Keep `max_results` focused; more results are useful only when synthesis needs breadth.

Answer guidance:

- Summarize the answer in the user's language.
- Cite source URLs returned by the tool.
- Separate confirmed facts from interpretation.
- Say when search results are thin, conflicting, or outdated.
