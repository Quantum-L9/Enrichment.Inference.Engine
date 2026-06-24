# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [governance]
# tags: [L9_TEMPLATE, agent, cursor, delta]
# owner: platform
# status: active
# token_estimate: 150
# ssot_for: [cursor-specific-delta]
# load_when: [cursor_code_gen, cursor_inline]
# references: [../AGENT.md, .cursorrules]
# --- /L9_META ---

# CURSOR.md — Cursor-Specific Delta

**VERSION**: 1.0.0 | **SHA_BASELINE**: 358d15d | **LAST_REVIEWED**: 2026-04-01

> Delta-only file. `.cursorrules` is the primary Cursor configuration. Load ../AGENT.md first.

## Workspace Settings

Cursor loads `.cursorrules` automatically from the repository root.
All 20 contracts are active in all Cursor sessions without manual loading.

**Enforcement:** The inline/terminal rules below are duplicated in `.cursorrules` under **CURSOR IDE — INLINE + TERMINAL** so the model applies them without `@`-ing this file.

## Inline Suggestion Constraints

- Never suggest FastAPI imports in engine modules (ARCH-001).
- Always suggest `sanitize_label()` wrapping for Cypher strings (C-07).
- Never autocomplete `Optional[`, `List[`, `Dict[` — use `T | None`, `list[T]`, `dict[K,V]` (C-05).
- Preserve the full handler signature when autocompleting in `handlers.py` (C-02).

## Permitted Commands

Run: `make agent-check`, `make agent-fix`, `make test`, `make audit`, `make verify`.
Never run: `make prod`, `make deploy`, `make dev-clean`, any `git push --force`.
