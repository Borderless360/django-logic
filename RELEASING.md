# Releasing Django Logic

A version tag publishes to PyPI and creates the matching GitHub release.
Branch pushes do not publish packages.

1. Merge the release commits into `master` through a pull request.
2. Set the package version in `pyproject.toml`.
3. Move the changelog's unreleased entries into a dated version section.
4. Run `python tests/manage.py test`, including metadata consistency checks.
   Run the PostgreSQL and Redis checks used by CI as well.
5. Run each consumer's validation against the candidate source. Include the
   public `django-logic-test` application's real-worker matrix. Do not publish
   while these checks fail.
6. Create and push the version tag:

   ```bash
   git tag -a vX.Y.Z -m "django-logic X.Y.Z"
   git push origin master vX.Y.Z
   ```

   [The publish workflow](.github/workflows/publish.yml) checks the tag against
   the package version, runs tests, builds the package, uploads it through
   PyPI trusted publishing, and creates release notes from the changelog.
   Watch the workflow and verify that it succeeds.
7. Install the published version in a clean virtual environment and import it.

Trusted publishing uses the repository's `pypi` environment and `publish.yml`
workflow. It does not require a long-lived PyPI token in the repository.
The workflow skips files and releases that already exist when a tag is rerun.
A published version cannot be replaced with different files.

If the workflow is unavailable, create a temporary token that day and use a new,
empty build directory. Run `uv build --out-dir <directory>`, then
`uvx twine check <directory>/*` and `uvx twine upload <directory>/*` on those same files.
Revoke the token immediately afterward. Never commit publishing credentials.
