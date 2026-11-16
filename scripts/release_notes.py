"""Read a dated changelog entry before publishing a version."""
from datetime import date
from pathlib import Path
import re
import sys


def release_notes(changelog, version):
    headings = list(re.finditer(r'^## \[([^\]]+)\](.*)$', changelog, re.MULTILINE))
    matches = [(index, heading) for index, heading in enumerate(headings) if heading[1] == version]
    if len(matches) != 1:
        raise ValueError(f'Expected one changelog entry for {version}.')
    index, heading = matches[0]
    dated = re.fullmatch(r'\s*—\s*(\d{4}-\d{2}-\d{2})\s*', heading[2])
    if not dated:
        raise ValueError(f'Changelog entry for {version} must have a release date.')
    date.fromisoformat(dated[1])
    end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog)
    notes = changelog[heading.end():end].strip()
    if not notes:
        raise ValueError(f'Changelog entry for {version} is empty.')
    return notes + '\n'


if __name__ == '__main__':
    print(release_notes(Path('CHANGELOG.md').read_text(), sys.argv[1]), end='')
