"""Carry application-owned request facts with a saved job."""
from contextlib import contextmanager
import json
import sys

from django_logic import conf
from django_logic.logger import transition_logger


class JobContextError(RuntimeError):
    """The configured adapter could not restore a job's application context."""


def capture_job_context():
    """Capture a separate JSON value without reading transition arguments."""
    adapter_class = conf.job_context()
    if adapter_class is None:
        return None
    value = adapter_class().capture()
    # Copy the value so later application mutations cannot change the saved context.
    return json.loads(json.dumps(value, allow_nan=False))


@contextmanager
def restored_job_context(job_id):
    """Restore application context for one attempt and its optional callbacks."""
    adapter_class = conf.job_context()
    if adapter_class is None:
        yield
        return
    from django_logic.background.models import TransitionMessage
    value = TransitionMessage.objects.filter(pk=job_id).values_list('job_context', flat=True).first()
    try:
        context = adapter_class().restore(value)
        context.__enter__()
    except Exception as error:
        raise JobContextError(
            f'JOB_CONTEXT could not restore the saved application context: '
            f'{type(error).__name__}: {error}'
        ) from error
    try:
        yield
    finally:
        try:
            # Cleanup cannot suppress work errors or change the recorded job result.
            context.__exit__(*sys.exc_info())
        except Exception:
            transition_logger.exception(
                'TransitionMessage#%s: JOB_CONTEXT cleanup failed. '
                'The original work result is unchanged.', job_id,
            )
