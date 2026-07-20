# Releasing django-logic

This project publishes to **PyPI** (https://pypi.org/project/django-logic/) and
cuts a matching **GitHub release** on the `Borderless360/django-logic` repo.

## Credentials

The PyPI upload token lives in **`.pypirc`** at the repo root. This file is
**git-ignored** (see `.gitignore`) and must **never** be committed. It holds a
project-scoped API token for `django-logic`:

```ini
[pypi]
username = __token__
password = pypi-…            # rotate at https://pypi.org/manage/account/token/
```

If the token is ever exposed, revoke it on PyPI and drop a fresh one into
`.pypirc` — nothing else needs to change.

## One-time tooling

Everything runs through [`uv`](https://github.com/astral-sh/uv) (build) and
`twine` via `uvx` (check + upload) — no global installs required.

## Release checklist

1. **Land all release commits on `master`** (via PR) and pull `master` locally.
2. **Set the version** in `pyproject.toml` (`[project].version`).
3. **Update `CHANGELOG.md`**: move `[Unreleased]` into a dated `[X.Y.Z]`
   section; leave a fresh empty `[Unreleased]`.
4. **Run the tests**: `python tests/manage.py test` (or `make test`).
5. **Check the consumer contract job is green**: the `Consumer contract (gv)`
   workflow (nightly + `workflow_dispatch`) runs gv's FSM test subset against
   django-logic@master. Trigger it manually for the release candidate and do
   not publish while it is red. (`gh run watch` with no argument prompts
   interactively and may pick the wrong run — resolve the id of the run you
   just dispatched first.)
   ```bash
   gh workflow run consumer-gv.yml
   sleep 5   # give the dispatch a moment to register
   gh run watch "$(gh run list --workflow=consumer-gv.yml --limit 1 --json databaseId -q '.[0].databaseId')" --exit-status
   ```
   NB: while the `GV_REPO_TOKEN` secret is not configured the job **skips
   with a warning and comes up green without testing anything** — a green
   run only means something once the secret exists (issue #119).
6. **Build + validate the artifacts**:
   ```bash
   make dist          # rm -rf dist/ build/ *.egg-info && uv build && twine check dist/*
   ```
7. **Publish to PyPI**:
   ```bash
   make publish       # uploads dist/* using .pypirc
   ```
8. **Tag and push**:
   ```bash
   git tag -a vX.Y.Z -m "django-logic X.Y.Z"
   git push origin master vX.Y.Z
   ```
9. **Create the GitHub release** with the changelog section as notes and the
   built artifacts attached:
   ```bash
   gh release create vX.Y.Z --title "django-logic X.Y.Z" \
     --notes-file <notes.md> --latest \
     dist/django_logic-X.Y.Z-py3-none-any.whl dist/django_logic-X.Y.Z.tar.gz
   ```
10. **Verify**: `pip install django-logic==X.Y.Z` in a clean venv imports cleanly.

> PyPI uploads are **irreversible** — a version number can never be re-uploaded.
> Always `make dist` + install-check before `make publish`.
