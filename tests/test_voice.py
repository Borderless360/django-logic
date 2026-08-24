"""The project writes in simplified English. This test keeps it that way.

A one-off rewrite decays. Every release since 0.4 added a little more private
dialect — numbered phases, "liveness", "retry horizon" — until a reader needed
the changelog to understand a comment. CLAUDE.md now states the vocabulary; this
test is what makes the statement hold.

It scans the library and the current documentation, not CHANGELOG.md. The
changelog is a historical record: it must keep naming the words and APIs that
shipped at the time, or it stops being true.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Words a reader cannot resolve without the project's own history. The
#: replacement is what to write instead — see CLAUDE.md for the full rule.
RETIRED_WORDS = {
    r'phase[ _-]?(?:1|2|one|two)\b': 'enqueue (save the row) or execute (run it on the worker)',
    r'\bliveness\b': 'say whether the row is still being retried',
    r'retry horizon': 'retry window',
    r'\bre-?drive\b': 're-dispatch',
    r'in-flight marker': 'the uncompleted row',
    r'speculative[ -]insert': 'describe the insert wait plainly',
    r'owning process\b': 'the process that declares the transition',
    r'finishing flight': 'an attempt that is still running',
    r'\bTM-scoped\b': 'scoped to the TransitionMessage row',
}

#: Text that must keep a retired word. Each entry is (path suffix, substring).
ALLOWED = (
    # A key a past release removed. checks.py compares it against a
    # consumer's settings dict by string, so the literal cannot change.
    ('django_logic/checks.py', "'PHASE2_STATE_GUARD'"),
    ('tests/test_removed_settings_check.py', 'PHASE2_STATE_GUARD'),
    # 0.14.0 renamed a log event. An operator whose alert matches the old
    # text needs to read the old text here to find that out.
    ('docs/logger.md', 'Before 0.14.0 that first line read'),
    # CLAUDE.md carries the table below — it has to name what it retires.
    ('CLAUDE.md', 'private dialect'),
)


def _defines_the_rule(path, line):
    """CLAUDE.md's retired-word table is the rule, not a breach of it."""
    return str(path).endswith('CLAUDE.md') and line.lstrip().startswith('|')


def _scanned_files():
    """Every file the rule covers: the library, and the docs a consumer reads."""
    for path in sorted((REPO_ROOT / 'django_logic').rglob('*.py')):
        if 'migrations' not in path.parts:
            yield path
    for name in ('README.md', 'CLAUDE.md', 'RELEASING.md'):
        yield REPO_ROOT / name
    yield from sorted((REPO_ROOT / 'docs').rglob('*.md'))


def _is_allowed(path, line):
    if _defines_the_rule(path, line):
        return True
    return any(
        str(path).endswith(suffix) and allowed in line
        for suffix, allowed in ALLOWED
    )


class SimplifiedEnglishTests(SimpleTestCase):
    def test_no_retired_words_in_the_library_or_the_docs(self):
        offences = []
        for path in _scanned_files():
            if not path.exists():
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                for pattern, replacement in RETIRED_WORDS.items():
                    if re.search(pattern, line, re.IGNORECASE) and not _is_allowed(path, line):
                        relative = path.relative_to(REPO_ROOT)
                        offences.append(
                            f'{relative}:{number} matches {pattern!r} — '
                            f'write {replacement} instead:\n    {line.strip()}'
                        )
        self.assertEqual(
            offences, [],
            'Retired words found. CLAUDE.md gives the replacement for each.\n\n'
            + '\n'.join(offences),
        )

    def test_the_scan_actually_reaches_the_engine(self):
        # A scan that silently matches nothing would pass forever. Pin the
        # two files most likely to grow dialect again.
        scanned = {p.name for p in _scanned_files()}
        self.assertIn('runner.py', scanned)
        self.assertIn('README.md', scanned)
