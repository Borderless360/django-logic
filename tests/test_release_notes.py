"""Publication requires the requested version's finished release notes."""
from unittest import TestCase

from scripts.release_notes import release_notes


class ReleaseNotesTests(TestCase):
    def test_extracts_only_the_requested_version_and_keeps_subheadings(self):
        text = '# Changelog\n\n## [5.1.0] — 2026-11-16\n\n### Fixed\n\n- A failure.\n\n## [5.0.0] — 2026-11-13\nOld notes.\n'
        self.assertEqual(release_notes(text, '5.1.0'), '### Fixed\n\n- A failure.\n')

    def test_refuses_unreleased_missing_empty_duplicate_and_invalid_dates(self):
        for text in (
            '## [5.1.0] — Unreleased\n- Pending.\n',
            '## [5.0.0] — 2026-11-13\n- Older.\n',
            '## [5.1.0] — 2026-11-16\n\n## [5.0.0] — 2026-11-13\n- Older.\n',
            '## [5.1.0] — 2026-11-16\n- First.\n## [5.1.0] — 2026-11-16\n- Second.\n',
            '## [5.1.0] — 2026-02-30\n- Invalid date.\n',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                release_notes(text, '5.1.0')
