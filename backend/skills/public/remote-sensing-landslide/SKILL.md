---
name: remote-sensing-landslide
description: 遥感影像滑坡识别、灾情研判与专题分析的领域默认技能
domain: remote-sensing-landslide
tools:
  - multi_kb_search
  - read_context_artifact
  - context_read
  - web_search
workflows:
  allowed:
    - full
  preferred: full
kb_policy:
  required: false
  binding_mode: agent_or_user_selected
---

# 遥感滑坡分析智能体

This generated Skill is the domain entry point for `remote-sensing-landslide`.

1. Load this Skill before domain retrieval.
2. Prefer `multi_kb_search` when authorized knowledge bases are selected.
3. Use `web_search` only for current external facts and retain source links.
4. Separate retrieved facts, engineering inference, and unresolved uncertainty.
