"""AI-readable failure output.

Formats a scenario's recorded timeline, plus the relevant TransitionMessage,
into a structured block that a human — or an AI agent — can read to see exactly
where the process diverged, without parsing stack traces or Django internals.
"""
from __future__ import annotations


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


def format_transition_message(transition_message) -> str:
    if transition_message is None:
        return ''
    return (
        '\n  TransitionMessage:\n'
        f'    transition: {transition_message.transition_name}\n'
        f'    is_completed: {transition_message.is_completed}\n'
        f'    errors_count: {transition_message.errors_count}\n'
        f'    last_error: {transition_message.last_error_message or "(none)"}'
    )


def format_failure(message: str, timeline: list[dict], *, transition_message=None) -> str:
    parts = [message, '', format_timeline(timeline)]
    message_block = format_transition_message(transition_message)
    if message_block:
        parts.append(message_block)
    return '\n'.join(parts)
