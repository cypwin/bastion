# Security Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a top-level `SECURITY.md` so GitHub surfaces a vulnerability-disclosure policy and researchers have a clear path to report.

**Architecture:** Single markdown file at repo root. No code changes. Rendered by GitHub on the Security tab after merge. Canonical repo is `cypwin/bastion` (verified in `pyproject.toml`).

**Tech Stack:** Markdown. No dependencies, no build step, no test framework involvement.

**Spec:** `docs/design/specs/2026-04-24-security-policy-design.md`

---

## Pre-requisites

Working tree: `.worktrees/security-md/` on branch `docs/security-policy`. Base commit: `12a6ebb`. No setup or dependency install needed.

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `SECURITY.md` | Top-level security policy (GitHub convention) |

No other files modified. `docs/security.md` is operator-facing hardening guidance and stays untouched per the spec's Non-Goals.

---

## Task 1: Create `SECURITY.md`

**Files:**
- Create: `SECURITY.md` (repo root)

- [ ] **Step 1: Write the file**

Create `SECURITY.md` at the repo root with this exact content:

````markdown
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
````

- [ ] **Step 2: Verify the file was written correctly**

Run: `wc -l SECURITY.md && head -5 SECURITY.md`

Expected: ~95 lines. First line is `# Security Policy`.

- [ ] **Step 3: Verify no existing `SECURITY.md` was clobbered**

Run: `git log --all --oneline -- SECURITY.md`

Expected: empty output (the file is new to the repo). If there's history, stop and reconcile — we may be overwriting a prior policy.

- [ ] **Step 4: Stage and commit**

Run:

```bash
git add SECURITY.md
git commit -m "$(cat <<'EOF'
docs: add SECURITY.md with vulnerability disclosure policy

GPSA as primary channel, email fallback, supported-versions table mirroring
docs/security.md, in/out-of-scope lists, safe harbor, credit policy.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

Expected: one commit, one file changed (~95 insertions), no hooks failing.

---

## Task 2: Pre-merge verification

No automated tests — this is a policy doc. These are manual sanity checks that must pass before merging to main.

**Files:** None modified. These steps validate Task 1's output.

- [ ] **Step 1: Confirm GPSA link matches canonical repo**

Run: `grep -E 'Repository|Homepage' pyproject.toml`

Expected: both values equal `https://github.com/cypwin/bastion`.

Then: `grep -c 'cypwin/bastion/security/advisories/new' SECURITY.md`

Expected: `1`. If 0, the link is wrong and the report button won't route.

- [ ] **Step 2: Confirm email matches the one in the spec**

Run: `grep -c 'cypmatwinkud@gmail.com' SECURITY.md`

Expected: `1`. If 0, either the spec or the file is wrong — reconcile.

- [ ] **Step 3: Confirm supported-versions table matches docs/security.md**

Run: `grep -A 6 '## Supported Versions' docs/security.md SECURITY.md`

Expected: both files list the same four rows (header, 0.4.x, 0.3.x, < 0.3). Any divergence here is a future source of user confusion — fix before merge.

- [ ] **Step 4: Smoke-test the email address (manual, user action)**

From a different mail account, send a one-line test message to `cypmatwinkud@gmail.com` with subject `BASTION SECURITY.md smoke test` and confirm it arrives. This is the only validation that the fallback channel actually works.

If the email bounces or never arrives, stop and fix the address in `SECURITY.md` and the spec before merging.

- [ ] **Step 5: Preview the rendered markdown**

Push the branch and view the file rendered by GitHub:

```bash
git push -u origin docs/security-policy
```

Then visit `https://github.com/cypwin/bastion/blob/docs/security-policy/SECURITY.md` and eyeball that:

- Headings render at correct levels
- Tables render (not as raw pipes)
- `:white_check_mark:` and `:x:` emoji render
- Both the GPSA link and the email are clickable
- Code blocks in the "What to include" section (if any) render correctly

If the render is broken, fix markdown syntax and re-commit — do not merge a broken policy.

---

## Task 3: Merge to main

**Files:** None. Branch-management task.

- [ ] **Step 1: Open pull request**

Run:

```bash
gh pr create --base main --title "docs: add SECURITY.md" --body "$(cat <<'EOF'
## Summary
- Adds top-level `SECURITY.md` with vulnerability disclosure policy
- GPSA primary, email (cypmatwinkud@gmail.com) fallback
- Supported versions mirrors docs/security.md (0.4.x + 0.3.x)
- In-scope / out-of-scope lists, safe harbor, credit policy

## Test plan
- [x] `grep` checks for canonical repo URL and email (Task 2 steps 1-2)
- [x] Supported-versions table matches docs/security.md (Task 2 step 3)
- [ ] Email smoke test received (Task 2 step 4)
- [ ] GitHub markdown render verified (Task 2 step 5)

Spec: docs/design/specs/2026-04-24-security-policy-design.md
EOF
)"
```

- [ ] **Step 2: Merge after review**

After PR review, merge via the GitHub UI or:

```bash
gh pr merge --squash --delete-branch
```

- [ ] **Step 3: Confirm Security tab shows the policy**

Visit `https://github.com/cypwin/bastion/security/policy` and confirm the content renders. GitHub usually picks this up within a minute.

- [ ] **Step 4: Confirm the "Report a vulnerability" button works**

Visit `https://github.com/cypwin/bastion/security` and click "Report a vulnerability". Confirm the form opens (you don't need to submit). If it's missing, Private Vulnerability Reporting may be disabled — enable it in Settings → Code security → Private vulnerability reporting.

---

## Done When

- `SECURITY.md` merged to `main`
- Security tab on GitHub renders the policy
- "Report a vulnerability" button is live
- Email smoke test received

## Non-Goals (reminder from spec)

- No changes to `docs/security.md`
- No dedicated `security@` alias (future one-line PR if desired)
- No bug bounty program
- No PGP key
