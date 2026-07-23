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
5. **Run the metadata drift check**: `python tests/manage.py test tests.test_metadata`
   — verifies that the Django trove classifiers, the CI test matrix, the
   `[project]` dependency floors, and the README support statement all agree
   (issues #144/#147). Do not publish while it is red: it means the package
   would advertise or resolve a Django/Python combination we don't test.
6. **Run the downstream consumer checks**: consumer-side validation lives
   with the consumers, not in this repo — a library should not know who
   consumes it. Before publishing, run each known downstream's suite against
   the release candidate from *its own* checkout/CI (install this repo at
   the candidate ref, e.g. `uv pip install --no-deps /path/to/django-logic`)
   and do not publish while any of them is red. The public validation rig
   (`django-logic-test`) exercises the release candidate on real
   broker/worker infrastructure and is the minimum bar.
7. **Build + validate the artifacts**:
   ```bash
   make dist          # rm -rf dist/ build/ *.egg-info && uv build && twine check dist/*
   ```
8. **Publish to PyPI**:
   ```bash
   make publish       # uploads dist/* using .pypirc
   ```
9. **Tag and push**:
   ```bash
   git tag -a vX.Y.Z -m "django-logic X.Y.Z"
   git push origin master vX.Y.Z
   ```
10. **Create the GitHub release** with the changelog section as notes and the
    built artifacts attached:
    ```bash
    gh release create vX.Y.Z --title "django-logic X.Y.Z" \
      --notes-file <notes.md> --latest \
      dist/django_logic-X.Y.Z-py3-none-any.whl dist/django_logic-X.Y.Z.tar.gz
    ```
11. **Verify**: `pip install django-logic==X.Y.Z` in a clean venv imports cleanly.

> PyPI uploads are **irreversible** — a version number can never be re-uploaded.
> Always `make dist` + install-check before `make publish`.
