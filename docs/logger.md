# Django-Logic Logging
## Loggers

The library provides two loggers:
- **`logger`**: Main logger for logging all activity of django-logic (`django-logic`)
- **`transition_logger`**: Special logger for logging only activity of transitions (`django-logic.transition`)

### Transition Log Format
```
timestamp tr_id <event_type> ...args
```

### Basic Transition Log Example
```
timestamp tr_id Start ProcessName TransitionName instance_key root_id parent_id     - first is declaration of the transition
timestamp tr_id Celery celery_task id celery_root_id              - if run into celery add more logs about it
timestamp tr_id Lock
timestamp tr_id SideEffect A                                      - side effect is started
timestamp tr_id SideEffect B                                      - new side effect means the previous one was completed
timestamp tr_id UnLock
timestamp tr_id Callback A                                        - a callback is started
timestamp tr_id Callback B                                        - new callback means the previous one was completed
timestamp tr_id Done                                              - transition is completed
```

## Celery Integration
Callbacks or SideEffects can be executed in a celery task. 
Moreover, each callback can be executed in its own celery task. 

### Example: All callbacks in a single celery task
```
...
timestamp tr_id CeleryCallbacks celery_root_id celery_parent_id celery_task_id 
timestamp tr_id Callback A
timestamp tr_id Callback B
...
```

### Example: Each callback in a separate celery task
```
...
timestamp tr_id Callback A celery_root_id celery_parent_id celery_task_id
timestamp tr_id Callback B celery_root_id celery_parent_id celery_task_id
...
```
**Note**: The same pattern applies for side effects and failure callbacks.

## Nested Transitions
One transition can be invoked inside another transition in side effects or callbacks.

### Example: Side effect A invokes transition B
```
timestamp tr_a_id Start ProcessName TransitionName instance_key root_id parent_a_id   - parent_a_id == tr_a_id
timestamp tr_a_id Lock
timestamp tr_a_id SideEffect A

timestamp tr_b_id Start ProcessName TransitionName instance_key root_id parent_a_id
timestamp tr_b_id Lock
timestamp tr_b_id UnLock
timestamp tr_b_id Done

timestamp tr_a_id UnLock
timestamp tr_a_id Done
```

## Future Considerations
**Note**: OpenTelemetry integration is under consideration.
