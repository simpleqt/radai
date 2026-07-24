---
name: html-ppt
description: HTML PPT Studio — author professional static HTML presentations in many styles, layouts, and animations, all driven by templates. Use when the user asks for a presentation, PPT, slides, keynote, deck, slideshow, "幻灯片", "演讲稿", "做一份 PPT", "做一份 slides", a reveal-style HTML deck, a 小红书 图文, or any kind of multi-slide pitch/report/sharing document that should look tasteful and be usable with keyboard navigation. Triggers include keywords like "presentation", "ppt", "slides", "deck", "keynote", "reveal", "slideshow", "幻灯片", "演讲稿", "分享稿", "小红书图文", "talk slides", "pitch deck", "tech sharing", "technical presentation".
domain: "*"
tools:
  - generate_html_ppt
---

# html-ppt — HTML PPT Studio

Generate previewable HTML PPT artifacts through the system tool
`generate_html_ppt`.

## Core rules

- The only final delivery tool is `generate_html_ppt`.
- Do not stop after `multi_kb_search` or `read_skill_reference`.
- Do not use `bash`, `present_file`, `present_files`, local scripts, or manual
  file operations for PPT delivery.
- If `multi_kb_search` is available and the topic needs domain facts, do one
  knowledge-base search first, then call `generate_html_ppt`.
- If `web_search` is available, use it only when knowledge-base evidence is
  clearly insufficient.

## Progressive reference loading

**Do NOT bulk-load references.** This skill is designed for just-in-time loading:

1. `read_local_skill_file("html-ppt")` loads this file once.
2. Pick template + theme from the tables below when the scenario is clear.
3. Call `read_skill_reference` **at most once per turn**, and **only one path**,
   when you truly cannot decide template/theme or presenter mode.
4. Never read `references/index.md` together with other references in the same turn.
5. Prefer compact refs (`references/*.md`). Load `references/manual/*` only for
   deep customization — not for standard `generate_html_ppt` delivery.

## Required workflow

When the user asks for PPT / slides / deck / 演讲稿 / 分享稿:

1. Infer topic, audience, and style from the user message.
2. Choose template + theme from **Recommended defaults** below.
3. Optional: one `read_skill_reference` call **only if** still uncertain:
   - unsure about theme → `references/themes.md`
   - unsure about template → `references/full-decks.md`
   - speaker notes / 演讲稿 → `references/presenter-mode.md`
4. If needed, do one `multi_kb_search`.
5. Immediately call `generate_html_ppt(...)`.

If the deck is not generated, the task is not complete.

## Tool contract

Call `generate_html_ppt` with:

- `template`: full-deck template name (see defaults below)
- `theme`: theme name (see defaults below)
- `title`: final PPT title
- `filename`: optional `.html` file name
- `slides_outline`: recommended; each slide separated by `---`

If `slides_outline` is omitted, the tool can build a fallback outline from the
latest user request.

## Recommended defaults

| scenario | template | theme |
|---|---|---|
| Technical sharing / engineering | `tech-sharing` | `blueprint` or `tokyo-night` |
| Formal report / academic topic | `weekly-report` | `academic-paper` or `corporate-clean` |
| Business presentation / pitch | `pitch-deck` | `corporate-clean` or `pitch-deck-vc` |
| Product launch | `product-launch` | `aurora` or `soft-pastel` |
| Presenter notes / speaker script | `presenter-mode-reveal` | `tokyo-night` |
| 小红书图文 | `xhs-post` or `xhs-white-editorial` | `xiaohongshu-white` |
| Safety / risk review | `testing-safety-alert` | `corporate-clean` |
| Architecture / system design | `knowledge-arch-blueprint` | `blueprint` or `engineering-whiteprint` |

Common templates: `tech-sharing`, `weekly-report`, `product-launch`, `pitch-deck`,
`presenter-mode-reveal`, `course-module`, `xhs-post`, `xhs-white-editorial`.

Common themes: `blueprint`, `tokyo-night`, `academic-paper`, `corporate-clean`,
`minimal-white`, `aurora`, `xiaohongshu-white`, `dracula`, `catppuccin-mocha`.

Invalid names fail fast — the tool returns the live list from disk.

## `slides_outline` format

```text
# 封面
一句副标题
- 关键词 1
- 关键词 2
---
# 目录
- 第一部分
- 第二部分
- 第三部分
---
# 核心内容
先用 1-2 句说明这一页要表达什么。
- 要点 A
- 要点 B
- 要点 C
备注：这一页适合口头补充实现细节
```

## Content restrictions

- Do not put raw URLs, Markdown image syntax, or long file links into
  `slides_outline`.
- If source materials contain figures, rewrite them as short text such as
  “建议插图：XXX 原理图” or “图示位置：系统框图”.
- Keep slide text presentation-friendly: use conclusions, labels, bullet
  points, and takeaways, not retrieval URLs.

## References (load one at a time, only when needed)

| path | load when |
|---|---|
| `references/themes.md` | theme choice unclear |
| `references/full-decks.md` | template choice unclear |
| `references/presenter-mode.md` | speaker notes / 演讲稿 needed |
| `references/manual/*` | rare deep customization only |

See `references/index.md` for the full catalog — do not load it routinely.
