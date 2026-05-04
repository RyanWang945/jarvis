---
name: social-search-guide
description: Use when answering questions about public discussion on X/Twitter or notable social posts.
when_to_use: User asks what people are saying on X/Twitter, asks for tweets, public reactions, social sentiment, or notable posts from specific X accounts.
tools:
  - x_search
tags:
  - search
  - social
  - summarization
---

# Social Search Guide

Use `x_search` when the user explicitly asks about X/Twitter posts, tweets, named X accounts, social reactions, or public sentiment on X.

Prefer ordinary web search when the user asks for verified facts, official announcements, news coverage, or sources outside social media.

Parameter guidance:

- Use `handles` when the user names specific accounts.
- Use `exclude_handles` only when the user asks to filter accounts out.
- Do not combine `handles` and `exclude_handles`.
- Use `date_from` and `date_to` for explicit time windows.
- Use `include_images` or `include_video` only when visual content matters.
- Keep `max_results` small for quick sentiment checks and larger for broader reaction scans.

Answer guidance:

- Separate facts from opinions and apparent sentiment.
- Include notable posts or cited claims when available.
- Mention source bias: X posts are public reactions, not representative polling.
- Include source links or citations returned by the tool.
