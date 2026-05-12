---
name: image-artifact-planner
description: Planner guidance for delegating image, SVG, diagram, chart, and visual artifact generation to Codex.
when_to_use: User asks Jarvis to generate an image, SVG, diagram, architecture chart, flow chart, visual asset, or picture file through Codex.
tools:
  - delegate_to_codex
capabilities:
  - image
  - svg
  - diagram
  - chart
  - picture
  - 图片
  - 图
  - 架构图
tags:
  - artifacts
  - image
  - svg
  - planner
---

# Image Artifact Planner

Use this skill when the user asks Jarvis to generate an image-like artifact, including raster images from image generation, SVG diagrams, architecture charts, flow charts, or visual files through `delegate_to_codex`.

Planner boundary:

- Delegate only the source artifact creation to Codex.
- Keep the Codex instruction narrow and file-oriented.
- Tell Codex the expected output path and format.
- Preserve the user's requested format. If the user asks for image gen, image generation, PNG, raster image, or a picture without saying SVG, ask Codex to create a PNG/WebP raster image, not an SVG.
- For raster image generation, tell Codex to use its image generation capability when available, then copy the final selected image into the Jarvis workspace before finishing.
- Ask Codex to report the generated or modified file path in its final response.
- Ask Codex to create a compact visual artifact that can be read in one Feishu image preview without scrolling.
- Prefer a small, high-signal diagram over a complete exhaustive map.
- Do not ask Codex to render, screenshot, convert, upload, or deliver the artifact unless the user explicitly requested a raster image generated through image gen.
- Do not ask Codex to launch browsers, Edge, Chrome, ImageMagick, Inkscape, Playwright, Selenium, or other renderers.
- Do not ask Codex to inspect or kill renderer processes.
- Do not ask Codex to clean temporary preview directories.
- Do not include phrases such as "verify the SVG renders", "take a screenshot", "convert to PNG", or "upload/send the image".

Visual complexity constraints:

- For image gen / raster outputs, target a landscape PNG suitable for a single Feishu image preview, preferably around `1536x1024` or similar. Keep text labels large and sparse because raster text can degrade.
- For SVG diagrams, target a compact canvas around `1200x800`; do not exceed `1400x900` unless the user explicitly asks for a large detailed poster.
- Use `viewBox` matching the canvas size and avoid layouts that require vertical or horizontal scrolling to understand.
- Limit the diagram to the most important 5-9 modules or groups.
- Use at most 2 hierarchy levels: high-level groups plus representative files/components.
- Prefer grouped boxes, short labels, and a small number of arrows.
- Avoid listing every file, directory, class, dependency, endpoint, or data table.
- Avoid dense text blocks, long tables, tiny fonts, nested boxes, or many crossing arrows.
- Use minimum readable text size around 13-16 px for SVG labels.
- If the architecture is large, ask Codex to summarize subsystems instead of expanding every detail.

Runtime boundary:

- Jarvis Runtime owns artifact discovery, validation, MIME checks, SVG-to-PNG preview generation when needed, and channel delivery.
- Feishu image upload is a channel concern after Runtime resolves attachments.
- If the user asks for an SVG that should appear in Feishu, Codex should still create the SVG only; Runtime will create the PNG preview if needed.
- If Codex image generation saves a raster output under Codex's default generated-images directory, Codex must copy the chosen final image into the Jarvis workspace, for example `docs/assets/<name>.png`, because Jarvis only sends attachments from allowed workspace/data roots.

Recommended `delegate_to_codex` instruction shape:

```text
Analyze the repository structure enough to create the requested visual artifact.
Create or update exactly one visual artifact at <path> in the user-requested format.
If the user requested image gen or a raster picture, use Codex image generation and save/copy the final PNG or WebP into the Jarvis workspace.
If the user requested SVG, create one compact SVG file with a 1200x800 style canvas, clear labels, and a concise layout.
Show only the key modules and relationships; do not include every file.
Do not upload, send, or clean preview files.
Do not run browser or image conversion tools.
Finish by reporting the artifact path.
```

For Chinese user requests, keep the generated visual labels in Chinese unless the user asks otherwise.
