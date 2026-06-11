# Presenter mode (compact)

Load `references/manual/presenter-mode.md` only when you need full authoring steps.

## When to use

Prefer `presenter-mode-reveal` template when the user mentions:

- 演讲 / 分享 / 讲稿 / 逐字稿 / speaker notes
- presenter view / 演讲者视图 / 演讲者模式
- 30–60 分钟分享、不想忘词、需要提词器

Static decks (小红书图文、图册、自己不讲) → **do not** use presenter mode.

## Quick action

```text
generate_html_ppt(
  template="presenter-mode-reveal",
  theme="tokyo-night",
  title="...",
  slides_outline="..."
)
```

Put oral script in `slides_outline` **备注** lines or notes blocks; keep slide bullets audience-facing.

## Theme cycling

`presenter-mode-reveal` supports T-key theme cycling. Good defaults: `tokyo-night`, `dracula`, `corporate-clean`.

## Adding notes to other templates

Any full-deck template can use `<aside class="notes">` per slide if `runtime.js` is loaded. See manual guide only when customizing non-default templates.
