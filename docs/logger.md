# Django-Logic Logging

State-change logging uses two standard Python loggers. Configure their
handlers through Django `LOGGING`, as you would for any library.

## Loggers

- **`django-logic`** — general library activity (the worker loop, the
  safety nets). Available in code as `from django_logic.logger import logger`.
- **`django-logic.transition`** — the per-transition lifecycle event log.
  Available as `from django_logic.logger import transition_logger`.

### Configure them

```python
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "django-logic": {"handlers": ["console"], "level": "INFO"},
        "django-logic.transition": {"handlers": ["console"], "level": "INFO"},
    },
}
```

## Logging transition kwargs (and redacting PII)

The `Start` lifecycle line attaches the transition's kwargs to the log
record's `extra` (`extra={'kwargs': ...}`), and the callback-failure log
does too. Those kwargs can include a `user` object and arbitrary business
data (amounts, emails, tokens), so scrub them in your
logging configuration (a `logging.Filter` on the `django-logic.transition`
logger) if the deployment is privacy-sensitive.

## Send buffered reports before a job process exits

Pull workers use `os._exit()` when each job process finishes. This skips
Python exit handlers. Buffered logs and telemetry may still be waiting to send.
The separate process that checks stuck jobs and deletes old records exits the same way.

Applications with buffered reporting can configure one function:

```python
DJANGO_LOGIC = {
    'JOB_PROCESS_FINISH': 'myapp.reporting.finish_job_process',
}
```

The function takes no arguments. It sends only reports belonging to the
current job process. It must not change business data.

```python
# myapp/reporting.py
def finish_job_process():
    collector.send_current_process_records()
```

`collector` is application code. The library does not provide a collector
or automatically flush logging handlers, queues, or Sentry.
The setting defaults to `None`, which keeps the existing exit behavior.

The library calls this function on a new daemon thread after engine work
finishes, including handled job failures. Report delivery has its own
two-second limit, even when the job has `timeout=None`.
The worker continues checking other jobs while reports are sent.
If sending blocks Python itself, the worker stops that job process.
Reporting errors or timeouts do not change the recorded job outcome or add retry errors.
This also applies when the finish function raises `SystemExit` or calls `os._exit()`.

Each collector must replace inherited buffers, private locks, sender threads,
and network clients before their first use in a job process.
Check the process ID before acquiring an inherited lock, or reset state with
`os.register_at_fork(after_in_child=...)`. Discard inherited worker records
from the job's copy; the worker still owns its original buffer.
Flush only the resulting process-local state. A standard handler's `flush()`
may do nothing, and it does not flush a separate telemetry client.
When several senders can block, start them independently within the shared
two-second limit. The finish function runs on a new thread, so it must not
depend on the thread-local state of a side effect.

Delivery is best effort. A crash, `SIGKILL`, or `os._exit()` during business
work skips the function. The library retains `os._exit()` after delivery
and does not run inherited Python exit handlers. Sync mode does not create
a job process and does not call this function.


## Transition identity and locks

A synchronous transition starts with this literal field order:

```text
tr_id Start ProcessName action_name instance_key root_id parent_id
```

Applications may use these fields to group activity by transition or record.
A background submission appends its queue information after these fields.
`state_hash` is also attached to the log record.

A refused state lock logs at INFO before raising:

```text
tr_id Lock failed instance_key — state is locked
```

Repeated `Lock failed` lines without an intervening `Unlock` for the same
record can indicate an unfinished transition or an expired owner that left a lock.
Check the running job and the configured lock timeout before retrying.
A successful release after failed validation logs
`Unlock instance_key after revalidation failure`.

`lock=False` logs `Lock skipped instance_key — lock=False` and releases no lock.
Background completion and failure logs describe saved job outcomes.
Callback errors are best effort and do not change those outcomes.
