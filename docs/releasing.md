# Releasing BASTION

This document describes the one-time setup required before the first
tag-triggered release, and the steps for cutting a new release.

## One-time GitHub repo setup

### 1. Create the `pypi` environment

The release workflow (`.github/workflows/release.yml`) publishes to PyPI
using OIDC trusted publishing. This requires a GitHub environment named
`pypi`.

1. In the GitHub repo, go to **Settings → Environments → New environment**.
2. Name it `pypi`.
3. (Optional) Add a required reviewer to gate publishes on manual approval.
4. No secrets need to be set — OIDC provides the token dynamically.

### 2. Register trusted publisher on PyPI

1. Create the PyPI project (first publish can be manual: `python -m build && twine upload dist/*`).
2. On PyPI, go to **Your projects → bastion → Settings → Publishing**.
3. Add a new trusted publisher:
   - Owner: `cypwin`
   - Repository name: `bastion`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

### 3. ghcr.io image visibility

Images are pushed to `ghcr.io/cypwin/bastion` by the `docker` job. After
the first push, visit **<https://github.com/users/cypwin/packages/container/bastion/settings>**
and set visibility to Public (if desired).

## Cutting a release

1. Ensure all PRs are merged and CI is green on `main`.
2. Update `CHANGELOG.md` with the new version.
3. Bump version in `pyproject.toml` and `src/bastion/__init__.py`.
4. Commit: `git commit -am "chore: bump version to X.Y.Z"`
5. Tag:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z — short description"
   ```
6. Push:
   ```bash
   git push origin main
   git push origin vX.Y.Z
   ```
7. Watch the `Release` workflow in GitHub Actions:
   - `pypi` runs first
   - `docker` runs only if `pypi` succeeds
   - `github-release` runs only if both succeed
8. Verify:
   - PyPI: `pip install bastion==X.Y.Z`
   - Docker: `docker pull ghcr.io/cypwin/bastion:X.Y.Z`
   - GitHub: check the Release page and auto-generated notes

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
VER=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
python -m build
```

### Expected outputs

`dist/` will contain two artifacts (with `$VER` substituted from `pyproject.toml`):

- `bastion_client-${VER}-py3-none-any.whl` — the wheel
- `bastion_client-${VER}.tar.gz` — the sdist

### Sanity checks

Confirm the wheel contains the expected modules:

```bash
python -m zipfile -l dist/bastion_client-${VER}-py3-none-any.whl
```

Expected: entries for `bastion_client/__init__.py`, `bastion_client/client.py`,
`bastion_client/models.py`, plus `*.dist-info/` metadata.

Install the wheel into a clean virtualenv and import it:

```bash
python -m venv /tmp/bc-test
source /tmp/bc-test/bin/activate
pip install dist/bastion_client-${VER}-py3-none-any.whl
python -c "from bastion_client import BastionClient; print(BastionClient)"
deactivate
rm -rf /tmp/bc-test
```

Expected: install succeeds, the `print` shows the class object. Cleanup runs
unconditionally, so a stale venv won't be left behind if `deactivate` fails.

### Cleanup

`dist/` and `bastion_client.egg-info/` are not committed. Delete them between
builds:

```bash
rm -rf dist bastion_client.egg-info
```

## Cutting a `bastion-client` release

`bastion-client` is published on its own cadence, independent of the main
`bastion` package. The tag-triggered workflow (`.github/workflows/release-client.yml`)
fires on tags matching `client-v*` and publishes to PyPI via OIDC trusted publishing.

### One-time setup

1. **Create the `pypi-client` GitHub environment.**
   In **Settings → Environments → New environment**, name it `pypi-client`.
   (Optional) add a required reviewer to gate publishes.

2. **Bootstrap the PyPI project.**
   Trusted publishing only attaches to an existing project, so the first
   release must be uploaded manually:

   ```bash
   cd clients/bastion-client
   python -m build
   python -m twine upload dist/*
   ```

3. **Register the trusted publisher on PyPI.**
   On PyPI, go to **Your projects → bastion-client → Settings → Publishing**
   and add:
   - Owner: `cypwin`
   - Repository name: `bastion`
   - Workflow name: `release-client.yml`
   - Environment name: `pypi-client`

   After this, the workflow takes over — no further manual uploads.

### Releasing a new version

1. Ensure CI is green on `main`.
2. Bump `version` in `clients/bastion-client/pyproject.toml`.
3. Update any client-relevant changelog notes.
4. Commit: `git commit -am "chore(client): bump version to X.Y.Z"`
5. Tag and push:
   ```bash
   git tag -a client-vX.Y.Z -m "bastion-client vX.Y.Z"
   git push origin main
   git push origin client-vX.Y.Z
   ```
6. Watch the `Release bastion-client` workflow in GitHub Actions:
   - `pypi` runs first
   - `github-release` runs only if `pypi` succeeds
7. Verify: `pip install bastion-client==X.Y.Z`
