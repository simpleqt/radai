# Full-deck templates (compact)

Use with `generate_html_ppt(template=..., theme=..., ...)`. For visual traits and inspiration, load `references/manual/full-decks.md` **only if** the table below is not enough.

## Scenario templates (most common)

| template | slides | when to use |
|---|---|---|
| `tech-sharing` | 8 | 技术分享、内部 talk、开发者会议 |
| `weekly-report` | 7 | 周报、团队状态、业务回顾 |
| `product-launch` | 8 | 产品发布、功能上线 keynote |
| `pitch-deck` | 10 | 融资路演、商业提案 |
| `presenter-mode-reveal` | 6 | **演讲/逐字稿/演讲者视图** — 用户要讲、需要 S 键 notes |
| `course-module` | 7 | 教学模块、工作坊、带侧边栏目标 |
| `xhs-post` | 9 | 小红书 3:4 图文 carousel |

## Style-forward templates

| template | when to use |
|---|---|
| `xhs-white-editorial` | 白底杂志风、中文图文 + 横向 deck |
| `xhs-pastel-card` | 柔和马卡龙、生活/慢内容 |
| `obsidian-claude-gradient` | GitHub 暗紫、MCP/Agent/dev-tool 教程 |
| `graphify-dark-graph` | 暗色知识图谱、CLI/data-viz 发布 |
| `knowledge-arch-blueprint` | 奶油蓝图、系统架构/数据流 |
| `hermes-cyber-terminal` | 暗终端、工具评测/honest review |
| `testing-safety-alert` | 红琥珀警示、安全/风险/事故复盘 |
| `dir-key-nav-minimal` | 极简 keynote、一页一观点 |

## Default pairings

- Technical sharing → `tech-sharing` + `blueprint` or `tokyo-night`
- Formal report → `weekly-report` + `academic-paper` or `corporate-clean`
- Business pitch → `pitch-deck` + `corporate-clean` or `pitch-deck-vc`
- Speaker script → `presenter-mode-reveal` + `tokyo-night`
- 小红书 → `xhs-post` or `xhs-white-editorial` + `xiaohongshu-white`

If the template name is invalid, `generate_html_ppt` returns the live template list from disk.
