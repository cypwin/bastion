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
   - Owner: `cyprian-w`
   - Repository name: `bastion`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

### 3. ghcr.io image visibility

Images are pushed to `ghcr.io/cyprian-w/bastion` by the `docker` job. After
the first push, visit **<https://github.com/users/cyprian-w/packages/container/bastion/settings>**
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
   - Docker: `docker pull ghcr.io/cyprian-w/bastion:X.Y.Z`
   - GitHub: check the Release page and auto-generated notes
