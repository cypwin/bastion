# BASTION Security Policy — Design Spec

> Tier 1 item #2 of production-readiness follow-up. Adds a top-level `SECURITY.md`
> so GitHub surfaces a vuln-reporting policy and researchers have a clear
> disclosure path.

## Goal

Ship a `SECURITY.md` at the repo root that:

1. Tells researchers *how* to report (GPSA primary, email fallback).
2. Tells them *what* we treat as a vulnerability vs. a bug.
3. Tells them which versions get security fixes.
4. Gives them enough legal comfort to report (safe harbor).
5. Commits to acknowledging them unless they prefer anonymity.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Primary disclosure channel | GitHub Private Security Advisories | Enabled by default on public repos; integrates with CVE flow; no extra infra |
| Fallback channel | Email: `cypmatwinkud@gmail.com` | Lowers barrier for researchers without GitHub accounts; address is already public in git history |
| Supported versions | 0.4.x and 0.3.x | Mirrors existing `docs/security.md` table |
| Response SLA | Best-effort, no hard numbers | Solo maintainer; hard SLAs become unkept promises |
| Safe harbor | Yes (good-faith research on own instances) | Standard OSS practice; removes a real reporting barrier |
| Credits | Yes, acknowledged unless anonymity requested | Standard courtesy; costs nothing |
| Bundled changes | `SECURITY.md` only (no docs/security.md edits) | Supported-versions table already lives there; duplication is fine, divergence risk is low |

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `SECURITY.md` | Top-level security policy (GitHub convention) |

No changes to `docs/security.md` (its content is operator-facing hardening guidance, not a disclosure policy — the two docs serve different audiences).

## `SECURITY.md` Section Outline

```markdown
# Security Policy

## Supported Versions
<table matching docs/security.md>

## Reporting a Vulnerability
<GPSA link + email fallback + what to include>

## Scope
### In Scope
<bulleted list from brainstorm>
### Out of Scope
<bulleted list from brainstorm>

## Response
<best-effort acknowledgment language>

## Safe Harbor
<good-faith research protection>

## Credit
<acknowledgment unless anonymity requested>
```

### In-scope list (verbatim from brainstorm)

- Auth/authz bypass on `/broker/*`, `/a2a/*`, or the proxy catch-all
- RCE, SSRF, path traversal, injection
- Rate-limit / trusted-proxy bypass
- Information disclosure via audit log, task store, or error messages
- A2A task-store or lease manipulation across tenants
- GPU/driver crash caused by a crafted HTTP request
- VRAM-exhaustion DoS bypassing budget enforcement

### Out-of-scope list (verbatim from brainstorm)

- Ollama vulnerabilities (report upstream)
- Issues requiring local admin / physical access to the host
- DoS via legitimate load without bypassing any limit
- Missing security headers on error responses
- Version disclosure in `/broker/status` (intentional, auth-gated)

## Key Language Notes

- **GPSA link:** `https://github.com/cyprian-w/bastion/security/advisories/new` (canonical repo URL per `pyproject.toml` and the `9d5f1da` unify-URLs commit).
- **Response SLA wording:** "We aim to acknowledge reports within a few business days. BASTION is maintained by a single developer; please allow reasonable time for investigation."
- **Safe harbor wording:** short, not a full legal license — "We will not pursue legal action against researchers who act in good faith, test only against their own instances, and do not access or exfiltrate data belonging to third parties."

## Testing

This is a policy doc, not code. Verification is:

1. **Renders correctly** on GitHub (headings, tables, code blocks).
2. **GPSA link resolves** to the repo's advisory form after merge.
3. **Security tab** on GitHub shows the policy after merge (GitHub auto-links `SECURITY.md`).
4. **Email address** is deliverable (sanity check: send yourself a test from another account).

No automated tests required; no pytest impact.

## Non-Goals

- Dedicated `security@` alias — noted as a possible future one-line PR; not blocking.
- Bug bounty program / paid rewards.
- Hall-of-fame page separate from advisory credits.
- Changes to `docs/security.md`.

## Risks

- **Email scraping:** `cypmatwinkud@gmail.com` will be scraped by bots. User explicitly accepted this since the address is already public in git commit metadata.
- **Unhandled reports:** A vulnerability report that falls through the cracks is worse than no policy. Mitigation: the best-effort language sets expectations honestly; GPSA also generates GitHub notifications that are harder to miss than email.
