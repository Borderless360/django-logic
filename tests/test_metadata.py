"""Metadata drift guard (issues #144/#147).

Published metadata once disagreed about supported Django versions: the
``[project]`` dependency said ``django>=4.0`` while the trove classifiers and
the CI matrix started at 4.2, and the README said "Django 4.0+" and pointed
users at a stale GitHub tag instead of PyPI. These tests pin the sources of
truth to each other so that kind of drift fails a release instead of shipping:

- classifiers  <->  CI test matrix (the set of tested Django versions)
- requires-python floor  <->  Python classifiers
- django dependency floor  <->  smallest Django classifier
- README support statement / install section  <->  all of the above

Pure file parsing — no database, no Django settings, nothing registered.
``pyproject.toml`` is parsed with the stdlib ``tomllib``; the CI workflow is
scraped with regex on purpose (no yaml dependency). Assertions compare
version SETS, never line numbers, so reformatting the files cannot break them.
"""
import re
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / 'pyproject.toml'
CI_WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'ci.yml'
README = REPO_ROOT / 'README.md'


def _version_tuple(version):
    """'4.2' -> (4, 2) for numeric comparison."""
    return tuple(int(part) for part in version.split('.'))


class MetadataDriftTests(unittest.TestCase):
    """pyproject classifiers, CI matrix, dependency floors, and README agree."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(PYPROJECT, 'rb') as fh:
            cls.pyproject = tomllib.load(fh)
        cls.project = cls.pyproject['project']
        cls.classifiers = cls.project['classifiers']
        cls.ci_text = CI_WORKFLOW.read_text()
        cls.readme_text = README.read_text()

    # -- helpers -------------------------------------------------------------

    @property
    def django_classifier_versions(self):
        return {
            match.group(1)
            for classifier in self.classifiers
            for match in [re.fullmatch(r'Framework :: Django :: (\d+\.\d+)', classifier)]
            if match
        }

    @property
    def python_classifier_versions(self):
        return {
            match.group(1)
            for classifier in self.classifiers
            for match in [re.fullmatch(r'Programming Language :: Python :: (3\.\d+)', classifier)]
            if match
        }

    @property
    def ci_django_versions(self):
        # Matrix entries look like: - { python-version: "3.12", django: "5.2" }
        # The django-main early-warning job installs from git and has no
        # `django: "X.Y"` key, so it is naturally excluded.
        return set(re.findall(r'django:\s*"(\d+\.\d+)"', self.ci_text))

    @property
    def django_dependency(self):
        deps = self.project['dependencies']
        django_deps = [
            dep for dep in deps
            if re.match(r'^django\s*[><=!~\[(;]', dep.strip(), re.IGNORECASE)
            or dep.strip().lower() == 'django'
        ]
        self.assertEqual(
            len(django_deps), 1,
            'expected exactly one bare "django" entry in [project] dependencies, '
            'found: %r' % (django_deps,))
        return django_deps[0]

    # -- 1. classifiers <-> CI matrix ----------------------------------------

    def test_django_classifiers_match_ci_matrix(self):
        classifier_versions = self.django_classifier_versions
        ci_versions = self.ci_django_versions
        self.assertTrue(classifier_versions, 'no Framework :: Django :: X.Y classifiers found')
        self.assertTrue(ci_versions, 'no django versions found in the CI matrix')
        self.assertEqual(
            classifier_versions, ci_versions,
            'Framework :: Django classifiers and the CI test matrix disagree: '
            'classifiers claim %s, CI tests %s. Every claimed version must be '
            'tested and every tested version must be claimed (#147).'
            % (sorted(classifier_versions), sorted(ci_versions)))

    # -- 2. requires-python floor <-> Python classifiers ---------------------

    def test_requires_python_floor_matches_python_classifiers(self):
        requires_python = self.project['requires-python']
        floor_match = re.search(r'>=\s*(\d+\.\d+)', requires_python)
        self.assertIsNotNone(
            floor_match,
            'requires-python (%r) has no ">=X.Y" floor' % requires_python)
        floor = floor_match.group(1)

        python_versions = self.python_classifier_versions
        self.assertTrue(python_versions, 'no Programming Language :: Python :: 3.X classifiers found')
        min_classifier = min(python_versions, key=_version_tuple)
        self.assertEqual(
            _version_tuple(floor), _version_tuple(min_classifier),
            'requires-python floor (%s) does not match the smallest Python '
            'classifier (%s)' % (floor, min_classifier))

    # -- 3. django dependency floor <-> Django classifiers -------------------

    def test_django_dependency_floor_matches_classifiers(self):
        dep = self.django_dependency
        floor_match = re.search(r'>=\s*(\d+\.\d+)', dep)
        self.assertIsNotNone(
            floor_match,
            'django dependency (%r) declares no ">=X.Y" floor, so pip may '
            'resolve untested Django versions (#147)' % dep)
        floor = floor_match.group(1)

        classifier_versions = self.django_classifier_versions
        self.assertTrue(classifier_versions, 'no Framework :: Django :: X.Y classifiers found')
        min_classifier = min(classifier_versions, key=_version_tuple)
        self.assertGreaterEqual(
            _version_tuple(floor), _version_tuple(min_classifier),
            'django dependency floor (%s) is below the smallest claimed '
            'classifier (%s): pip could install a Django version the project '
            'neither tests nor claims (%r)' % (floor, min_classifier, dep))
        # The concrete, current contract: floor at 4.2 (4.0/4.1/5.0 support
        # was dropped in 0.5.0 — see CHANGELOG).
        self.assertIn(
            '>=4.2', dep.replace(' ', ''),
            'django dependency (%r) must exclude versions below 4.2' % dep)

    # -- 4. README agrees and carries no stale install instructions ----------

    def test_readme_states_the_same_django_floor(self):
        self.assertIn(
            'Django 4.2', self.readme_text,
            'README.md no longer states the "Django 4.2" support floor — '
            'keep it in sync with pyproject.toml and CI (#147)')

    def test_readme_has_no_stale_release_instructions(self):
        for stale in ('legacy 0.1.x', '@v0.4.0'):
            self.assertNotIn(
                stale, self.readme_text,
                'README.md still contains the stale install/release string '
                '%r — the current release is published to PyPI (#144)' % stale)
