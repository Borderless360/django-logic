# TODO

Planned changes for upcoming versions of django-logic.

---

## 0.3.0

### Remove deprecated code
- [ ] Remove legacy logging: `LogType`, `AbstractLogger`, `DefaultLogger`, `NullLogger`, `get_logger()`
- [ ] Remove settings `DJANGO_LOGIC_DISABLE_LOGGING` and `DJANGO_LOGIC_CUSTOM_LOGGER`
- [ ] Remove all `self.logger` references from commands, transitions, process

### DRF as an optional dependency
- [ ] Move `djangorestframework` from hard dependencies to `[project.optional-dependencies]` (the core library does not import DRF)


### Logging from State instead of Transition
- [ ] Move log calls to `State` methods instead of `Transition` (TODO in `transition.py:181`)

### NextTransition via callbacks
- [ ] Consider executing next_transition via a callback instead of a separate step (TODO in `transition.py:192`)

### UUID for Actions
- [ ] Add UUID generation (`tr_id`) for `Action.change_state` (TODO in `transition.py:281`)

### Exceptions in root transition
- [ ] Revisit behaviour: currently root transition swallows exceptions and returns `tr_id` — this contradicts the documentation which shows `try/except TransitionNotAllowed`

### PyPI
- [ ] Set up automated publishing via GitHub Actions (on tag push)

### Demo
- [ ] Move demo to `django-logic-demo` repo
