# Interactive and automatic requests share business functions

The same export rules apply whether a person requests an export or a schedule starts it.
Write those rules once in application functions.
Two ordinary transition declarations can choose different queues for that work.

```python
from django_logic import Process
from django_logic.background import BackgroundTransition

from exports.actions import build_export


class ExportProcess(Process):
    transitions = [
        BackgroundTransition(
            action_name='build_for_user',
            sources=['pending', 'failed'],
            in_progress_state='building',
            target='ready',
            failed_state='failed',
            queue='interactive_exports',
            side_effects=[build_export],
        ),
        BackgroundTransition(
            action_name='build_scheduled',
            sources=['pending', 'failed'],
            in_progress_state='building',
            target='ready',
            failed_state='failed',
            queue='scheduled_exports',
            side_effects=[build_export],
        ),
    ]
```

The interactive endpoint calls `export.process.build_for_user()`.
The scheduler calls `export.process.build_scheduled()`.
Each call validates permissions and conditions before saving a job.
Workers for the interactive queue can serve user requests without waiting behind scheduled exports.
The deployment chooses worker capacity for each queue.
Both paths use the same business function, saved-job recovery and completion states.

An accepted request is not a completed export.
The user interface shows `building`, then `ready` or `failed` from the saved business record.
Use an ordinary synchronous `Transition` when the caller must receive the completed result before returning.
Do not make that decision depend on whether the caller happens to be a test.

Keep the business function independent of queue names and worker configuration.
Use application records to preserve results that a retry must reuse.
Use `atomic=False` only when independently committed progress or provider confirmations must survive a later failure.
That choice changes transaction behavior; it does not change the export's business rules.

Required steps belong in the owning job's `side_effects` when they share one completion result.
Related records can have their own ordinary background transitions.
Save their submissions and the parent's required check together, then let that check wait for their recorded results.
See [parent and child processes](nested-processes.md).
Optional callbacks can report observations, but must not be the only place that submits required work.

`sync_execution()` runs the same saved jobs inline for tests.
It does not repeatedly run postponed work until every related job finishes.
Tests that exercise a waiting parent should run its pending job again after the child jobs finish.
Check both saved business results and job outcomes.
