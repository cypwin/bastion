# `bastion-client` Publish-Prep — Design Spec

> Tier 1 item #3 of production-readiness follow-up. Resolves the half-published
> state of `bastion-client` by making the worktree branch a self-contained
> "ready to publish when you press the button" state. No external resources
> consumed in this work; PyPI publication is a deliberately separate future PR.

## Goal

Eliminate the misleading `pip install bastion-client` instructions in user-facing
docs without deleting the (working, tested) `clients/bastion-client/` package.
Set the project up so that when the maintainer is ready to publish, the work is
small and isolated — and so that a 2nd-machine reviewer can build + verify the
wheel locally without any external account.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Path forward | Publish (eventual), not purge | Package is 775 LOC of working, tested code with its own `pyproject.toml`, MIT licensed, version 0.2.0 — deletion would discard real work and remove a useful abstraction |
| Where to stop on this branch | Local-only readiness | User explicitly does not want any external resource consumed yet; wants to test on another device first |
| Install instructions on this branch | `pip install ./clients/bastion-client/` (path install) | Makes the docs accurate to what's actually installable today; flips to PyPI command in the future publish PR |
| Examples primary path | `BastionClient` (restored) | The whole point of having a client library is to abstract httpx boilerplate; using raw httpx in examples while the library exists is incoherent |
| Examples fallback path | Show raw httpx as the "if you don't want the dep" alternative | Documents the contract so users without the client can still drive the broker |
| Release workflow | Defer to a separate publish PR | Keeps this branch free of any PyPI-runtime references; user can review it as a focused unit when ready |
| Version | Stay at 0.2.0 | Nothing ships, nothing needs bumping |
| `docs/releasing.md` extension | Add a "Building bastion-client locally" section | Provides a reproducible recipe for the second-device verification step |

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `examples/python-client/example.py` | Restore `BastionClient` as primary; reverts the `httpx`-direct portion of `f23bbe2` |
| Modify | `examples/python-client/README.md` | `pip install bastion-client` → `pip install ./clients/bastion-client/`; mention `BastionClient` is local-install only until v0.3 publish |
| Modify | `examples/priority-tiers/multi_client.py` | Restore `BastionClient` as primary; raw httpx kept as a documented fallback or removed if redundant with code path |
| Modify | `examples/priority-tiers/README.md` | Same install-instruction change; "without bastion-client, set the header directly" becomes accurate fallback again |
| Modify | `docs/releasing.md` | Append a "Building bastion-client locally" subsection (commands + expected outputs + sanity checks) |

No changes to `clients/bastion-client/` itself. No CI changes. No `pyproject.toml` changes anywhere.

## What This Branch Does NOT Do

These are deliberate non-goals — they belong in the future publish PR:

- Create or modify `.github/workflows/release-client.yml`
- Change `bastion-client` version (stays at 0.2.0)
- Modify `pyproject.toml` of either package
- Configure PyPI trusted publishing
- Any `git push`, `gh pr create`, or `twine upload`
- Any commit to `main`

## Local Build Verification — what `docs/releasing.md` will document

The new subsection's content (verbatim outline; full prose lives in the implementation plan):

1. **Prerequisites:** Python 3.11+, `python -m pip install build`. No PyPI account required.
2. **Build command:**
   ```bash
   cd clients/bastion-client
   python -m build
   ```
3. **Expected outputs in `dist/`:**
   - `bastion_client-0.2.0-py3-none-any.whl`
   - `bastion_client-0.2.0.tar.gz`
4. **Sanity checks:**
   - `python -m zipfile -l dist/bastion_client-0.2.0-py3-none-any.whl` — confirm `bastion_client/client.py`, `bastion_client/models.py`, `bastion_client/__init__.py` are present.
   - `pip install dist/bastion_client-0.2.0-py3-none-any.whl` (in a clean venv) — confirm install succeeds.
   - `python -c "from bastion_client import BastionClient; print(BastionClient)"` — confirm import works.
5. **Cleanup note:** delete `dist/` and `*.egg-info/` between builds; they're not committed.

The point of this section: a reviewer on a different machine can validate the package is publishable *without* needing PyPI credentials.

## Examples Restoration — design notes

**Pre-`f23bbe2` state** (what to recover):
- `examples/python-client/example.py` used `BastionClient` directly
- `examples/priority-tiers/multi_client.py` used `BastionClient` for tier-aware requests

**Current state on `main`** (what to change from):
- Both examples switched to `httpx.AsyncClient` directly to avoid the unpublished-import error

**Target state on this branch:**
- Both examples use `BastionClient` as the primary path
- README install instructions point at `clients/bastion-client/` (path install) on this branch

The implementation plan will inspect `f23bbe2` (`git show f23bbe2 -- examples/`) to get the exact pre-state and restore it minus any cruft.

## Testing

This is a docs/examples + small `releasing.md` addition. No production code changes; no new tests required.

Verification per task is manual:

1. **Examples actually run** against a live BASTION when `BastionClient` is path-installed. Smoke test: `cd examples/python-client && pip install ../../clients/bastion-client && python example.py` (assumes BASTION running on default port).
2. **Local build recipe in `docs/releasing.md` works.** Run the documented commands, confirm the listed outputs are produced.
3. **No PyPI references remain** outside `docs/releasing.md`'s clearly-labeled "future publish" section. Search: `grep -rn 'pip install bastion-client' --include='*.md' --include='*.py'` should return only the future-publish section.

## Relationship to the release-hardening audit

The 2026-04-24 release-hardening audit identified "Publishing `bastion-client` to PyPI" as a deferred non-critical item, with rationale: *"Not urgent while examples don't depend on it (Task 16 made them httpx-only)."*

This spec **changes that premise**: by restoring `BastionClient` as the primary path in `examples/python-client/example.py` and `examples/priority-tiers/multi_client.py`, the examples once again depend on the package. Consequence:

- **Until the future publish PR ships,** users running the examples must first path-install the client (`pip install ./clients/bastion-client/`). The example READMEs document this step explicitly so the install path is honest at every commit.
- **The future publish PR moves from "nice to have" to "blocks examples-as-PyPI-install"** — once we publish, we flip the install command from path to PyPI in the same PR, and the friction disappears.

This is a deliberate trade: better-documented examples now (using the abstraction the package was designed to provide) at the cost of a slightly more involved future publish PR.

## Risks

- **Examples drift.** If `BastionClient`'s API changes after this work, the restored examples could break before publish. Mitigation: examples should be exercised in CI (the `bastion-client` test step in `e658c8d` covers the package itself but not the example scripts). Out of scope for this branch — flag as a future improvement.
- **Path-install instructions confuse new users.** Mitigation: the README change explicitly notes "until the first PyPI publish" so users understand it's a temporary state.
- **Future publish PR has high coordination cost.** Mitigation: this branch is intentionally small; the publish PR can re-use the recipe in `docs/releasing.md` for its own validation.

## Done When

- Both example scripts use `BastionClient` as primary
- All four user-facing READMEs (the two example READMEs, plus any others that say `pip install bastion-client`) reflect the path-install reality
- `docs/releasing.md` has a working "Building bastion-client locally" subsection
- `git grep "pip install bastion-client"` returns only the future-publish doc context
- The branch has no `git push` or PR action — purely local state
