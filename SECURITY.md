# Security Policy

BASTION's maintainer takes security reports seriously. This document describes
how to disclose vulnerabilities and what to expect in return.

## Supported Versions

Security fixes are released for the following versions:

| Version | Supported          |
| ------- | ------------------ |
| 0.4.x   | :white_check_mark: |
| 0.3.x   | :white_check_mark: |
| < 0.3   | :x:                |

Older versions receive no updates. Please upgrade before reporting issues
against unsupported releases.

## Reporting a Vulnerability

**Preferred: GitHub Private Security Advisory**

Open a private advisory at
[github.com/cypwin/bastion/security/advisories/new](https://github.com/cypwin/bastion/security/advisories/new).
GitHub's private advisory flow keeps the report confidential until a fix is
ready, and makes CVE coordination straightforward.

**Fallback: Email**

If you prefer email, send your report to **cypmatwinkud@gmail.com**. PGP is
not currently supported — if you need encrypted transport, ask first and
we'll arrange a channel.

**What to include in your report**

- Affected version(s) and commit hash if you have it
- Component: proxy / admin API / A2A / scheduler / audit / etc.
- Reproduction steps or proof-of-concept
- Impact assessment (what does the attacker gain?)
- Any suggested mitigation

Please do **not** file a public GitHub issue for security reports.

## Scope

### In scope

- Authentication or authorization bypass on `/broker/*`, `/a2a/*`, or the
  proxy catch-all (`/api/*`, `/v1/*`)
- Remote code execution, SSRF, path traversal, or injection
- Rate-limit bypass or `trusted_proxies` bypass
- Information disclosure via audit logs, task store, or error messages
  (e.g., API tokens leaking into logs)
- A2A task-store or lease manipulation across tenants
- **GPU/driver crash caused by a crafted HTTP request.** BASTION's core
  claim is preventing concurrent-access crashes; a request that bypasses
  the protection and hangs the driver is a real security issue for any
  shared deployment.
- VRAM-exhaustion DoS that bypasses configured budget enforcement

### Out of scope

These are bugs — not vulnerabilities. Please file a regular issue.

- Vulnerabilities in Ollama itself (report upstream to the Ollama project)
- Issues requiring local administrator access or physical access to the host
- Denial of service via legitimate load without bypassing any configured
  limit
- Missing security headers on error responses
- Version disclosure in `/broker/status` (intentional, auth-gated)

## Response

BASTION is maintained by a single developer. We aim to acknowledge reports
within a few business days, but cannot commit to hard SLAs. Investigation and
remediation timelines depend on severity and complexity. We'll keep you
informed as we triage, develop a fix, and coordinate disclosure.

## Safe Harbor

We will not pursue legal action against researchers who:

- Act in good faith and make a reasonable effort to avoid privacy violations,
  data destruction, or service disruption
- Test only against their own instances of BASTION
- Do not access, modify, or exfiltrate data belonging to other people
- Give us a reasonable opportunity to address the issue before public
  disclosure

This is not a formal legal license, but a good-faith commitment that
legitimate research is welcome.

## Credit

Unless you ask to remain anonymous, we will credit you in the advisory, in
`CHANGELOG.md`, and in any commit messages fixing the reported issue. If you
want to be credited under a handle other than your real name, just say so in
your report.
