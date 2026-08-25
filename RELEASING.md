# Releasing django-logic

This project publishes to **PyPI** (https://pypi.org/project/django-logic/) and
creates a matching **GitHub release** on the `Borderless360/django-logic` repo.

## Credentials

The PyPI upload token lives in **`.pypirc`** at the repo root. Git ignores this
file (see `.gitignore`). Never commit it. It holds a project-scoped API token
for `django-logic`:

```ini
[pypi]
username = __token__
password = pypi-…            # rotate at https://pypi.org/manage/account/token/
```

If the token leaks, revoke it on PyPI and write a new one into `.pypirc`.
Nothing else changes.

## One-time tooling

[`uv`](https://github.com/astral-sh/uv) builds the package. `uvx` runs `twine`
to check and upload it. You need no global installs.

## Release checklist

1. **Merge every release commit into `master`** through a pull request, then
   pull `master` locally.
2. **Set the version** in `pyproject.toml` (`[project].version`).
3. **Update `CHANGELOG.md`**: move `[Unreleased]` into a dated `[X.Y.Z]`
   section; leave a fresh empty `[Unreleased]`.
4. **Run the tests**: `python tests/manage.py test` (or `make test`).
5. **Run the metadata drift check**: `python tests/manage.py test tests.test_metadata`
   — it checks that the Django trove classifiers, the CI test matrix, the
   `[project]` dependency floors, and the README support statement agree.
   Do not publish while it is red. A red run means the package advertises or
   resolves a Django or Python version that the tests never run.
6. **Run the downstream consumer checks**: each consumer keeps its own
   validation, because a library should not know who consumes it. Install this
   repo at the candidate ref from the consumer's own checkout or CI
   (`uv pip install --no-deps /path/to/django-logic`), then run that
   consumer's suite. Do not publish while any of them is red. Always run the
   public validation rig (`django-logic-test`): it exercises the release
   candidate on real worker dynos against PostgreSQL and Redis, with induced
   crashes, timeouts and deploys. Its matrix is the release gate.
7. **Build the artifacts** — the workflow builds its own to upload; these
   are the copies attached to the GitHub release:
   ```bash
   make dist          # rm -rf dist/ build/ *.egg-info && uv build && twine check dist/*
   ```
8. **Tag and push. The tag publishes.**
   ```bash
   git tag -a vX.Y.Z -m "django-logic X.Y.Z"
   git push origin master vX.Y.Z
   ```
   `.github/workflows/publish.yml` refuses a tag whose name disagrees with
   the packaged version, runs the suite, builds, and uploads through PyPI
   trusted publishing. Watch it: `gh run watch`.

   `make publish` still works and stays as the fallback for the day the
   workflow cannot run. It needs the token in `.pypirc`; the workflow needs
   no credential at all.
9. **Create the GitHub release** with the changelog section as notes and the
    built artifacts attached:
    ```bash
    gh release create vX.Y.Z --title "django-logic X.Y.Z" \
      --notes-file <notes.md> --latest \
      dist/django_logic-X.Y.Z-py3-none-any.whl dist/django_logic-X.Y.Z.tar.gz
    ```
10. **Check the published package**: `pip install django-logic==X.Y.Z` in a
    clean virtualenv, then import it.

## Publishing from the tag

`.github/workflows/publish.yml` builds and uploads on a `v*` tag. It refuses
a tag whose name disagrees with the packaged version, runs the test suite
first, and skips files PyPI already holds, so re-running a tag is safe.

It holds no PyPI credential. GitHub mints a short-lived token for the job
and PyPI verifies it against a publisher record naming this repository, so
there is nothing in the repo or in CI secrets to leak or rotate.

That record is configured (verified on the `v0.17.1` tag, 2026-08-25: the
job logged `Found and verified trusted root`, generated attestations, and
uploaded). If it ever needs rebuilding, it is: owner `Borderless360`,
repository `django-logic`, workflow **`publish.yml`** — the filename, not
the `name:` inside the file — and environment `pypi`, plus a `pypi`
environment in the repository settings.

0.17.0 and 0.17.1 were published by hand, before this worked. Everything
after them goes out on the tag.

> A PyPI upload is final. You can never upload the same version number twice,
> so the tag is the point of no return. Re-running a tag is safe — the job
> passes `skip-existing`, so it will not fail on files PyPI already holds.
