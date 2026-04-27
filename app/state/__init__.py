"""State machine + lifecycle event log.

Public API:
    from app.state import (
        PortalStatus, ProductionStatus, LifecycleEvent,
        EventSource, InvalidTransitionError,
        transition,
    )

Invariant: nothing outside this package mutates `imports.portal_status` or
`imports.production_status`. Every mutation goes through `transition()`,
which appends a lifecycle event and projects the new state in the same
SQLite transaction.
"""
from app.state.events import (
    TransitionResult,
    append_event,
    list_events,
    replay_state,
    transition,
)
from app.state.machine import (
    EventSource,
    InvalidTransitionError,
    LifecycleEvent,
    PortalStatus,
    ProductionStatus,
)

__all__ = [
    "EventSource",
    "InvalidTransitionError",
    "LifecycleEvent",
    "PortalStatus",
    "ProductionStatus",
    "TransitionResult",
    "append_event",
    "list_events",
    "replay_state",
    "transition",
]
