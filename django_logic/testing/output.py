"""AI-readable failure output.

Formats a scenario's recorded timeline (plus the relevant TransitionMessage and,
optionally, a reproducible snapshot) into a structured block that a human — or
an AI agent — can read to see exactly where the process diverged, without
parsing stack traces or Django internals.
"""
from __future__ import annotations

import json


def format_timeline(entries: list[dict]) -> str:
    if not entries:
        return '  Timeline: (empty)'
    lines = ['  Timeline:']
    width = max(len(e.get('label', '')) for e in entries)
    for i, e in enumerate(entries, 1):
        label = e.get('label', '').ljust(width)
        outcome = e.get('outcome', '')
        detail = e.get('detail', '')
        line = f'    [{i}] {label}  -> {outcome}'
        if detail:
            line += f'  {detail}'
        lines.append(line)
    return '\n'.join(lines)


def format_tm(tm) -> str:
    if tm is None:
        return ''
    return (
        '\n  TransitionMessage:\n'
        f'    transition: {tm.transition_name}\n'
        f'    is_completed: {tm.is_completed}\n'
        f'    errors_count: {tm.errors_count}\n'
        f'    last_error: {tm.last_error_message or "(none)"}'
    )


def format_failure(message: str, timeline: list[dict], *, tm=None, snapshot=None) -> str:
    parts = [message, '', format_timeline(timeline)]
    tm_block = format_tm(tm)
    if tm_block:
        parts.append(tm_block)
    if snapshot is not None:
        parts.append(
            '\n  Snapshot (copy to reproduce with from_snapshot()):\n    '
            + json.dumps(snapshot, default=str)
        )
    return '\n'.join(parts)
