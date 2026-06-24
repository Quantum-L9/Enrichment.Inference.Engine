# Guardrails — AI Safety Constraints

**LAST_REVIEWED:** 2026-04-11

## Runtime / Transport Safety

- Production transport ingress is owned by the SDK runtime in `app/main.py`.
- Do not reintroduce production dispatch through:
  - `chassis/envelope.py`
  - `chassis/router.py`
  - `chassis/registry.py`
- Deprecated compatibility artifacts may remain in the repo, but they are not the constitutional transport path.
- Inter-node communication must route through SDK/Gate transport paths, not ad hoc peer HTTP shims.

## Enrichment Pipeline

### Prompt construction
- Prompts built via approved prompt-building paths should require structured output that matches the schema in use.
- Do not inject unsanitized free-text CRM fields into LLM prompts.
- Keep provider payloads within configured timeouts and variation limits.

### Output validation
- Orchestrator paths must validate/normalize responses before writeback or downstream sync.
- Consensus and variation thresholds must not be silently weakened without review.

### Field writes (Odoo / CRM integrations)
- Use explicitly allowlisted fields and governance-approved writeback paths only.
- No elevated/sudo-style writes without documented human approval.
- Writes must follow validated enrichment outputs and tenant/data rules.

## API Security

### Authentication
- Protected routes require the API key / auth flow implemented in the runtime/app layer.
- Health and metadata routes may be unauthenticated by design. Do not place sensitive data in those responses.
- API key material lives in env vars only — never source code or logs.

### Input validation
- Requests must be validated before enrichment or writeback paths execute.
- Tenant and object targeting must follow approved boundaries.

### Rate limits
- Concurrency, backoff, and breaker limits must remain enforced in the runtime/orchestration stack.

## AI Agent Constraints

### What agents MAY do autonomously
- Add tests for existing behavior
- Fix lint/type issues
- Improve docs
- Tighten contract alignment
- Regenerate governance files to match live runtime truth

### What agents MUST NOT do without human review
- Modify authentication or authorization logic
- Change the signal/schema contract
- Modify `.github/workflows/`
- Change default consensus / variation / timeout defaults without review
- Add new external API integrations
- Reintroduce deprecated local chassis dispatch as production behavior
- Modify `CODEOWNERS`, `AGENTS.md`, `CLAUDE.md`, or this file without governance review

## Secrets

- Zero hardcoded credentials
- `.env` is gitignored
- `.env.example` contains placeholders only
- Never log API keys, partner data, or raw LLM responses at INFO level or above
