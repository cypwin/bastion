# `bastion-client` Publish-Prep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore `BastionClient` as the primary path in shipped examples, fix install instructions to be accurate without PyPI, and document a local build recipe — all without consuming any external resources (no PyPI, no `git push`, no PR).

**Architecture:** Pure docs/examples work. Two Python example scripts get re-pointed at `bastion_client` (undoing the in-code half of `f23bbe2`). Four READMEs get install-instruction fixes. `docs/releasing.md` gets a new subsection. `clients/bastion-client/` itself is unchanged.

**Tech Stack:** Python 3.11+, `httpx`, `pydantic` (used by the existing `bastion_client` package), Markdown. No new dependencies.

**Spec:** `docs/design/specs/2026-04-25-bastion-client-publish-prep-design.md`

---

## Pre-requisites

Working tree: `.worktrees/bastion-client/` on branch `chore/bastion-client-resolution`. Base commit: `1f85a0d` (spec commit) on top of `12a6ebb`.

Canonical Python: `python` (per `CLAUDE.md`).

Don't `git push`, don't `gh pr create`, don't run `twine`. The spec explicitly defers all of these to a future publish PR.

The pre-`f23bbe2` versions of the two example scripts are the reference target for restoration. The plan embeds the full target content of each file — you do not need to consult git history while executing.

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Modify (rewrite) | `examples/python-client/example.py` | Use `BastionClient` as primary; restored to pre-`f23bbe2` semantics with updated prereq docstring |
| Modify | `examples/python-client/README.md` | `pip install bastion-client` → `pip install ./clients/bastion-client/` |
| Modify (rewrite) | `examples/priority-tiers/multi_client.py` | Use `BastionClient` as primary; restored to pre-`f23bbe2` semantics with updated prereq docstring |
| Modify | `examples/priority-tiers/README.md` | Same install-instruction fix; "without bastion-client, set header directly" wording becomes the documented fallback again |
| Modify | `docs/releasing.md` | Append "Building bastion-client locally" subsection |

No changes to `clients/bastion-client/` (package source). No CI changes. No `pyproject.toml` changes.

---

## Task 1: Restore `BastionClient` in the python-client example

**Files:**
- Modify: `examples/python-client/example.py` (rewrite)
- Modify: `examples/python-client/README.md` (install section only)

- [ ] **Step 1: Rewrite `examples/python-client/example.py`**

Overwrite the file with this exact content:

```python
"""BASTION Python client — basic usage example.

Prerequisites:
    # Until bastion-client is published to PyPI, install from source:
    pip install ./clients/bastion-client/

    Then run from anywhere (BASTION_URL env var optional, defaults to
    http://127.0.0.1:11434).

Make sure BASTION is running and a model is available
(e.g., ollama pull llama3.1:8b).
"""
from __future__ import annotations

import asyncio

from bastion_client import BastionClient


async def main() -> None:
    async with BastionClient() as client:
        # Check GPU/VRAM status
        vram = await client.check_vram()
        print(f"VRAM: {vram.used_vram_gb:.1f}/{vram.total_vram_gb:.1f} GB")
        print(f"Utilization: {vram.utilization_pct:.0f}%")
        print(f"Loaded models: {vram.loaded_models}")
        print()

        # Run inference with interactive priority
        print("Sending inference request...")
        result = await client.infer(
            "llama3.1:8b",
            "Explain what a GPU broker does in one sentence.",
            tier="interactive",
        )
        print(f"Response: {result['response']}")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Update install section in `examples/python-client/README.md`**

Find this block in the README:

````markdown
## Install

```bash
pip install bastion-client
# Or from source:
pip install ../../clients/bastion-client/
```
````

Replace it with:

````markdown
## Install

`bastion-client` is not yet published to PyPI. Install from source:

```bash
pip install ../../clients/bastion-client/
```

(Once published, this becomes `pip install bastion-client`.)
````

- [ ] **Step 3: Verify the file parses as valid Python**

Run:

```bash
python -c "import ast; ast.parse(open('examples/python-client/example.py').read())"
```

Expected: no output, exit code 0. If syntax is broken, fix before continuing.

- [ ] **Step 4: Verify imports resolve when the package is path-installed**

Run:

```bash
PYTHONPATH=clients/bastion-client python -c "from bastion_client import BastionClient; print('ok')"
```

Expected output: `ok`. If `ImportError`, the package source has drifted from the example's expectations — stop and reconcile.

- [ ] **Step 5: Commit**

Run:

```bash
git add examples/python-client/example.py examples/python-client/README.md
git commit -m "$(cat <<'EOF'
examples(python-client): restore BastionClient as primary path

Reverts the in-code half of f23bbe2 — the example once again uses the
client library it was written to demonstrate. Install instructions now
point at the path install (pip install ./clients/bastion-client/),
which is honest until the package is published to PyPI.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

Expected: one commit, two files changed.

---

## Task 2: Restore `BastionClient` in the priority-tiers demo

**Files:**
- Modify: `examples/priority-tiers/multi_client.py` (rewrite)
- Modify: `examples/priority-tiers/README.md` (install section + fallback wording)

- [ ] **Step 1: Rewrite `examples/priority-tiers/multi_client.py`**

Overwrite the file with this exact content:

```python
"""Priority tiers demo — shows BASTION scheduling order.

Launches three concurrent clients at different priority tiers.
Interactive requests are served before pipeline and background,
even when submitted at the same time.

Prerequisites:
    # Until bastion-client is published to PyPI, install from source:
    pip install ./clients/bastion-client/

Make sure BASTION is running on localhost:11434 and a model is available
(e.g., ollama pull llama3.1:8b).
"""
from __future__ import annotations

import asyncio
import time

from bastion_client import BastionClient

MODEL = "llama3.1:8b"
PROMPT = "Reply with exactly one word: hello."


async def send_request(name: str, tier: str, start_time: float) -> None:
    """Send a single inference request and report timing."""
    async with BastionClient() as client:
        print(f"[{name}] Submitting request (tier={tier})...")
        result = await client.infer(MODEL, PROMPT, tier=tier)
        elapsed = time.monotonic() - start_time
        response = result["response"].strip()[:80]
        print(f"[{name}] Completed in {elapsed:.1f}s (tier={tier}): {response}")


async def main() -> None:
    print("Priority Tiers Demo")
    print("=" * 50)
    print()
    print("Submitting 3 concurrent requests at different priorities.")
    print("Watch the completion order — interactive should finish first.")
    print()

    start = time.monotonic()

    # Launch all three concurrently.
    # BASTION's scheduler serves higher priority tiers first.
    await asyncio.gather(
        send_request("Background ", "background", start),    # priority 10
        send_request("Pipeline   ", "pipeline", start),      # priority 25
        send_request("Interactive", "interactive", start),    # priority 100
    )

    total = time.monotonic() - start
    print()
    print(f"All requests completed in {total:.1f}s")
    print()
    print("If BASTION had a queue backlog, interactive would be served first,")
    print("then pipeline, then background. With an idle broker, all three may")
    print("complete in similar time since there is no contention.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Update install section in `examples/priority-tiers/README.md`**

Find this block:

````markdown
## Install

```bash
pip install bastion-client
```
````

Replace it with:

````markdown
## Install

`bastion-client` is not yet published to PyPI. Install from source:

```bash
pip install ../../clients/bastion-client/
```

(Once published, this becomes `pip install bastion-client`.)
````

- [ ] **Step 3: Verify the fallback wording still makes sense**

The same README contains a section that explains how to set the priority header without using `bastion-client`. After the install change above, that fallback text should still be valid (it describes setting the `X-Broker-Priority` header on a raw HTTP request).

Run:

```bash
grep -A 5 'X-Broker-Priority\|set the header directly\|without `bastion-client`' examples/priority-tiers/README.md
```

Expected: the fallback section still appears and references `X-Broker-Priority`. If it references `X-Bastion-Priority` or anything else, that's a header-name drift bug — leave a note and fix in Task 4 verification.

- [ ] **Step 4: Verify the file parses**

Run:

```bash
python -c "import ast; ast.parse(open('examples/priority-tiers/multi_client.py').read())"
```

Expected: no output, exit code 0.

- [ ] **Step 5: Commit**

Run:

```bash
git add examples/priority-tiers/multi_client.py examples/priority-tiers/README.md
git commit -m "$(cat <<'EOF'
examples(priority-tiers): restore BastionClient as primary path

Reverts the in-code half of f23bbe2 for the priority-tiers demo. The
README's "without bastion-client, set the header directly" fallback
remains accurate as documentation for users who don't want the dep.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

Expected: one commit, two files changed.

---

## Task 3: Add "Building bastion-client locally" subsection to `docs/releasing.md`

**Files:**
- Modify: `docs/releasing.md` (append a new subsection)

- [ ] **Step 1: Find the right insertion point**

Run:

```bash
grep -n '^## ' docs/releasing.md
```

Expected: a list of top-level sections. The new subsection lands at the very end of the file (after the existing "Cutting a release" section). It is **not** a peer of "Cutting a release" — it's a separate `## Building bastion-client locally` section, since publishing `bastion-client` is a different lifecycle from `bastion`.

- [ ] **Step 2: Append the new section**

Append this exact content to `docs/releasing.md` (preserving any trailing newline):

````markdown

## Building bastion-client locally

> The `bastion-client` package at `clients/bastion-client/` is not yet
> published to PyPI. The instructions below let you build a wheel locally
> and verify it on a clean environment — useful for testing the package
> on a second machine before committing to publication.

### Prerequisites

- Python 3.11+
- `pip install build` (the [PyPA `build` tool](https://pypi.org/project/build/))

No PyPI account, no trusted-publishing setup, no network upload needed.

### Build

```bash
cd clients/bastion-client
python -m build
```

### Expected outputs

`dist/` will contain two artifacts:

- `bastion_client-0.2.0-py3-none-any.whl` — the wheel
- `bastion_client-0.2.0.tar.gz` — the sdist

### Sanity checks

Confirm the wheel contains the expected modules:

```bash
python -m zipfile -l dist/bastion_client-0.2.0-py3-none-any.whl
```

Expected: entries for `bastion_client/__init__.py`, `bastion_client/client.py`,
`bastion_client/models.py`, plus `*.dist-info/` metadata.

Install the wheel into a clean virtualenv and import it:

```bash
python -m venv /tmp/bc-test && source /tmp/bc-test/bin/activate
pip install dist/bastion_client-0.2.0-py3-none-any.whl
python -c "from bastion_client import BastionClient; print(BastionClient)"
deactivate && rm -rf /tmp/bc-test
```

Expected: install succeeds, the `print` shows the class object.

### Cleanup

`dist/` and `bastion_client.egg-info/` are not committed. Delete them between
builds:

```bash
rm -rf dist bastion_client.egg-info
```

### When to publish

Publication is deliberately a separate workflow. When you are ready to
publish `bastion-client` to PyPI:

1. Configure trusted publishing on PyPI for the `bastion-client` project
   (analogous to the `bastion` setup documented earlier in this file).
2. Add a `.github/workflows/release-client.yml` that builds and uploads on
   a `client-vX.Y.Z` tag.
3. Bump the version in `clients/bastion-client/pyproject.toml`.
4. Flip the install instructions in `examples/python-client/README.md` and
   `examples/priority-tiers/README.md` from path-install back to
   `pip install bastion-client`.
5. Push the tag.

Each of those steps is small in isolation; bundling them into one focused PR
keeps the publication review cohesive.
````

- [ ] **Step 3: Verify the section renders**

Run:

```bash
grep -c '^## Building bastion-client locally' docs/releasing.md
```

Expected: `1`. If `0`, the append didn't land — re-do Step 2.

Run:

```bash
grep -c 'python -m build' docs/releasing.md
```

Expected: `1`. (Used to confirm the new content is there, not just the heading.)

- [ ] **Step 4: Commit**

Run:

```bash
git add docs/releasing.md
git commit -m "$(cat <<'EOF'
docs(releasing): document local build for bastion-client

Adds a "Building bastion-client locally" section with the canonical
build command (python -m build), expected dist/ outputs, sanity checks
(wheel inspection + clean-venv install), cleanup, and a numbered list
of the steps the future publish PR will need to cover.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

Expected: one commit, one file changed.

---

## Task 4: Verification sweep

No code changes. These are checks that what we just did is internally consistent and doesn't leave broken instructions anywhere.

**Files:** None modified.

- [ ] **Step 1: Confirm no stale `pip install bastion-client` references remain in user-facing paths**

Run:

```bash
grep -rn 'pip install bastion-client' --include='*.md' --include='*.py' . \
  | grep -v _archive \
  | grep -v docs/superpowers \
  | grep -v 'docs/releasing.md'
```

Expected: empty output.

What we **expect to find** (and explicitly tolerate):
- Hits inside `_archive/` — old artifacts, not user-facing
- Hits inside `docs/design/specs/` and `docs/design/plans/` — internal design history
- The single hit in `docs/releasing.md`'s future-publish numbered list, where the string is shown as the *eventual* post-publish command

If anything else matches, edit that file to use the path-install form before continuing.

- [ ] **Step 2: Confirm both example scripts import the client at the top level**

Run:

```bash
grep -E '^from bastion_client import' examples/python-client/example.py examples/priority-tiers/multi_client.py
```

Expected: two matches, one in each file.

- [ ] **Step 3: Run the existing `bastion-client` tests to confirm we didn't break the package**

Run from the repo root:

```bash
cd clients/bastion-client && python -m pytest tests/ -v && cd -
```

Expected: all tests pass. We didn't modify package source, so this should be green.

- [ ] **Step 4: Build the wheel locally and inspect it (validates the new docs section)**

Run:

```bash
cd clients/bastion-client && python -m build && cd -
ls clients/bastion-client/dist/
python -m zipfile -l clients/bastion-client/dist/bastion_client-0.2.0-py3-none-any.whl
```

Expected:
- `dist/` contains `bastion_client-0.2.0-py3-none-any.whl` and `bastion_client-0.2.0.tar.gz`
- The wheel listing shows `bastion_client/__init__.py`, `bastion_client/client.py`, `bastion_client/models.py`

If `python -m build` reports the `build` package isn't installed, install it into the env first:

```bash
python -m pip install build
```

- [ ] **Step 5: Confirm the path-install round-trips**

Run:

```bash
python -m venv /tmp/bc-roundtrip
/tmp/bc-roundtrip/bin/pip install clients/bastion-client/dist/bastion_client-0.2.0-py3-none-any.whl
/tmp/bc-roundtrip/bin/python -c "from bastion_client import BastionClient; print(BastionClient)"
rm -rf /tmp/bc-roundtrip clients/bastion-client/dist clients/bastion-client/bastion_client.egg-info
```

Expected: install succeeds, the import prints `<class 'bastion_client.client.BastionClient'>`. The cleanup leaves the worktree free of build artifacts.

- [ ] **Step 6: Final git status check**

Run:

```bash
git status --short
git log --oneline main..HEAD
```

Expected:
- `git status` is empty (no leftover artifacts in the working tree)
- `git log` shows three new commits ahead of main: the python-client restore, the priority-tiers restore, and the docs/releasing.md addition

If `git status` shows leftover files (e.g., `dist/`, `*.egg-info/`), delete them before declaring done.

---

## Done When

- Both example scripts use `BastionClient` as primary
- Both example READMEs show `pip install ./clients/bastion-client/`
- `docs/releasing.md` has a working "Building bastion-client locally" section
- `grep -rn 'pip install bastion-client' --include='*.md' --include='*.py'` shows only the tolerated hits described in Task 4 Step 1
- `python -m build` produces both wheel and sdist
- The wheel installs cleanly into an isolated venv and imports successfully
- Three new commits on `chore/bastion-client-resolution` ahead of `main`
- No `git push`, no PR, no PyPI activity

## Non-Goals (reminder from spec)

- No changes to `clients/bastion-client/` source
- No `.github/workflows/release-client.yml`
- No version bump
- No PyPI trusted-publishing setup
- No external network activity of any kind
