"""Read retained job counts and timings without changing or running jobs."""
from collections import defaultdict
from math import ceil

from django_logic.background.models import TransitionMessage


_GROUP_FIELDS = (
    'queue_name', 'app_label', 'model_name', 'process_name',
    'owning_process_class', 'transition_name', 'parent_transition_name', 'effect_name',
)


def _percentiles(values):
    values.sort()
    if not values:
        return {'samples': 0, 'p50': None, 'p90': None, 'max': None}
    return {
        'samples': len(values),
        'p50': values[ceil(len(values) * 0.5) - 1],
        'p90': values[ceil(len(values) * 0.9) - 1],
        'max': values[-1],
    }


def transition_statistics(*, since, queues=None, using=None):
    """Summarize retained jobs created at or after ``since``.

    Counts cover each selected job's recorded history, not events within the
    window. Initial wait uses its first recorded start. Duration covers only
    its final measured attempt. Missing historical measurements stay unknown.
    Percentiles use the nearest rank over the available samples.
    """
    rows = TransitionMessage.objects.using(using).filter(created__gte=since)
    if queues is not None:
        rows = rows.filter(queue_name__in=queues)
    groups = {}
    waits, durations = defaultdict(list), defaultdict(list)
    fields = _GROUP_FIELDS + (
        'created', 'first_started_at', 'duration_ms', 'is_completed',
        'ended_in_failure', 'completion_kind', 'errors_count',
        'start_attempt_count', 'deferral_count',
    )
    for row in rows.order_by().values(*fields).iterator():
        key = tuple(row[field] for field in _GROUP_FIELDS)
        if key not in groups:
            groups[key] = {
                **dict(zip(_GROUP_FIELDS, key)),
                'jobs': 0, 'pending': 0, 'succeeded': 0, 'failed': 0,
                'other_completed': 0, 'errors': 0, 'recorded_starts': 0,
                'repeated_starts': 0, 'deferrals': 0,
            }
        group = groups[key]
        group['jobs'] += 1
        if not row['is_completed']:
            group['pending'] += 1
        elif row['ended_in_failure']:
            group['failed'] += 1
        elif row['completion_kind'] == 'succeeded':
            group['succeeded'] += 1
        else:
            group['other_completed'] += 1
        group['errors'] += row['errors_count']
        group['recorded_starts'] += row['start_attempt_count']
        group['repeated_starts'] += max(0, row['start_attempt_count'] - 1)
        group['deferrals'] += row['deferral_count']
        if row['first_started_at'] is not None:
            waits[key].append(round((row['first_started_at'] - row['created']).total_seconds() * 1000, 3))
        if row['duration_ms'] is not None:
            durations[key].append(row['duration_ms'])
    return [
        {**group, 'initial_wait_ms': _percentiles(waits[key]),
         'last_attempt_duration_ms': _percentiles(durations[key])}
        for key, group in sorted(groups.items())
    ]
