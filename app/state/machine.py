"""State machine — enums and pure transition table.

Two parallel state dimensions per pedido:
    PortalStatus      — portal-side lifecycle (parsed → sent_to_fire → cancelled).
    ProductionStatus  — production lifecycle (none → requested → in_production → completed).

Both projected into columns on `imports`; both derivable from
`order_lifecycle_events`.

Vocabulary of every event the system will ever emit is locked here, even
events whose transitions are only wired in later phases. This prevents drift
and lets Phase 1 tests assert exhaustiveness.

The `transition()` API lives in `app.state.events` because it touches the DB.
This module is pure — only enums and the transition tables.
"""
from __future__ import annotations

from enum import Enum


class PortalStatus(str, Enum):
    PARSED = "parsed"               # in human review, not yet in Fire
    SENT_TO_FIRE = "sent_to_fire"   # CAB_VENDAS row created
    CANCELLED = "cancelled"         # killed in portal before reaching Fire
    ERROR = "error"                 # parsing/import-time failure (terminal)


class ProductionStatus(str, Enum):
    NONE = "none"                              # Fire-only; production not started
    REQUESTED = "production_requested"         # outbox enqueued for Gestor
    IN_PRODUCTION = "in_production"            # Gestor accepted, Apontaê running
    COMPLETED = "completed"                    # terminal: production done
    PRODUCTION_CANCELLED = "production_cancelled"  # cancelled mid-production


class EventSource(str, Enum):
    PORTAL = "portal"      # action originated in the portal UI / CLI
    FIRE = "fire"          # observed in the Firebird ERP (poll worker)
    GESTOR = "gestor"      # webhook from Gestor de Produção
    APONTAE = "apontae"    # forwarded telemetry from Apontaê
    SYSTEM = "system"      # internal worker event (retry, dead-letter, etc.)


class LifecycleEvent(str, Enum):
    """Vocabulary of events. Some transitions are wired in later phases (see TRANSITIONS)."""
    # Phase 1 — what the codebase already does today
    IMPORTED = "imported"
    SEND_TO_FIRE_SUCCEEDED = "send_to_fire_succeeded"
    SEND_TO_FIRE_FAILED = "send_to_fire_failed"
    CANCELLED = "cancelled"
    PARSE_FAILED = "parse_failed"

    # Phase 3 — outbox / Gestor de Produção
    POST_TO_GESTOR_REQUESTED = "post_to_gestor_requested"
    POST_TO_GESTOR_SENT = "post_to_gestor_sent"
    POST_TO_GESTOR_FAILED = "post_to_gestor_failed"

    # Phase 4 — webhooks back from Gestor / Apontaê
    PRODUCTION_UPDATE = "production_update"      # partial update; can fire N times
    PRODUCTION_COMPLETED = "production_completed"
    PRODUCTION_CANCELLED = "production_cancelled"

    # Phase 5 — poll worker
    FIRE_STATUS_CHANGED = "fire_status_changed"


class InvalidTransitionError(Exception):
    """The (current_state, event) pair is not in the transition table."""

    def __init__(
        self,
        portal_status: PortalStatus,
        production_status: ProductionStatus,
        event: LifecycleEvent,
    ) -> None:
        super().__init__(
            f"Invalid transition: cannot apply {event.value} when "
            f"portal={portal_status.value} production={production_status.value}"
        )
        self.portal_status = portal_status
        self.production_status = production_status
        self.event = event


# (current_portal, event) -> new_portal
# Absence means the event is not allowed in that portal_status.
PORTAL_TRANSITIONS: dict[tuple[PortalStatus, LifecycleEvent], PortalStatus] = {
    # Phase 1 — covers commit_preview / send-to-fire / cancel paths
    (PortalStatus.PARSED, LifecycleEvent.IMPORTED): PortalStatus.PARSED,
    (PortalStatus.PARSED, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED): PortalStatus.SENT_TO_FIRE,
    (PortalStatus.PARSED, LifecycleEvent.SEND_TO_FIRE_FAILED): PortalStatus.PARSED,
    (PortalStatus.PARSED, LifecycleEvent.CANCELLED): PortalStatus.CANCELLED,
    (PortalStatus.PARSED, LifecycleEvent.PARSE_FAILED): PortalStatus.ERROR,

    # Phase 3+ — once an order is in Fire, gestor/production events do not move portal_status
    (PortalStatus.SENT_TO_FIRE, LifecycleEvent.POST_TO_GESTOR_REQUESTED): PortalStatus.SENT_TO_FIRE,
    (PortalStatus.SENT_TO_FIRE, LifecycleEvent.POST_TO_GESTOR_SENT): PortalStatus.SENT_TO_FIRE,
    (PortalStatus.SENT_TO_FIRE, LifecycleEvent.POST_TO_GESTOR_FAILED): PortalStatus.SENT_TO_FIRE,
    (PortalStatus.SENT_TO_FIRE, LifecycleEvent.FIRE_STATUS_CHANGED): PortalStatus.SENT_TO_FIRE,
    (PortalStatus.SENT_TO_FIRE, LifecycleEvent.PRODUCTION_UPDATE): PortalStatus.SENT_TO_FIRE,
    (PortalStatus.SENT_TO_FIRE, LifecycleEvent.PRODUCTION_COMPLETED): PortalStatus.SENT_TO_FIRE,
    (PortalStatus.SENT_TO_FIRE, LifecycleEvent.PRODUCTION_CANCELLED): PortalStatus.SENT_TO_FIRE,
}

# (current_production, event) -> new_production
PRODUCTION_TRANSITIONS: dict[tuple[ProductionStatus, LifecycleEvent], ProductionStatus] = {
    # Phase 1
    (ProductionStatus.NONE, LifecycleEvent.IMPORTED): ProductionStatus.NONE,
    (ProductionStatus.NONE, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED): ProductionStatus.NONE,
    (ProductionStatus.NONE, LifecycleEvent.SEND_TO_FIRE_FAILED): ProductionStatus.NONE,
    (ProductionStatus.NONE, LifecycleEvent.CANCELLED): ProductionStatus.NONE,
    (ProductionStatus.NONE, LifecycleEvent.PARSE_FAILED): ProductionStatus.NONE,

    # Phase 3 — outbox to Gestor
    (ProductionStatus.NONE, LifecycleEvent.POST_TO_GESTOR_REQUESTED):
        ProductionStatus.REQUESTED,
    (ProductionStatus.REQUESTED, LifecycleEvent.POST_TO_GESTOR_SENT):
        ProductionStatus.IN_PRODUCTION,
    (ProductionStatus.REQUESTED, LifecycleEvent.POST_TO_GESTOR_FAILED):
        ProductionStatus.REQUESTED,

    # Phase 5 — poll fire / status reflect
    (ProductionStatus.NONE, LifecycleEvent.FIRE_STATUS_CHANGED):
        ProductionStatus.NONE,
    (ProductionStatus.REQUESTED, LifecycleEvent.FIRE_STATUS_CHANGED):
        ProductionStatus.REQUESTED,
    (ProductionStatus.IN_PRODUCTION, LifecycleEvent.FIRE_STATUS_CHANGED):
        ProductionStatus.IN_PRODUCTION,

    # Phase 4 — webhooks from Gestor with partial updates (idempotent, status stays)
    (ProductionStatus.IN_PRODUCTION, LifecycleEvent.PRODUCTION_UPDATE):
        ProductionStatus.IN_PRODUCTION,
    (ProductionStatus.IN_PRODUCTION, LifecycleEvent.PRODUCTION_COMPLETED):
        ProductionStatus.COMPLETED,
    (ProductionStatus.IN_PRODUCTION, LifecycleEvent.PRODUCTION_CANCELLED):
        ProductionStatus.PRODUCTION_CANCELLED,
}

TERMINAL_PORTAL_STATES: frozenset[PortalStatus] = frozenset(
    {PortalStatus.CANCELLED, PortalStatus.ERROR}
)
TERMINAL_PRODUCTION_STATES: frozenset[ProductionStatus] = frozenset(
    {ProductionStatus.COMPLETED, ProductionStatus.PRODUCTION_CANCELLED}
)


def is_valid(
    portal_status: PortalStatus,
    production_status: ProductionStatus,
    event: LifecycleEvent,
) -> bool:
    """True iff event is accepted by both axes from the given state."""
    return (
        (portal_status, event) in PORTAL_TRANSITIONS
        and (production_status, event) in PRODUCTION_TRANSITIONS
    )


def apply_event(
    portal_status: PortalStatus,
    production_status: ProductionStatus,
    event: LifecycleEvent,
) -> tuple[PortalStatus, ProductionStatus]:
    """Pure: compute new (portal, production) given current state + event.

    Raises InvalidTransitionError if the transition is not defined.
    """
    portal_key = (portal_status, event)
    prod_key = (production_status, event)
    if portal_key not in PORTAL_TRANSITIONS or prod_key not in PRODUCTION_TRANSITIONS:
        raise InvalidTransitionError(portal_status, production_status, event)
    return PORTAL_TRANSITIONS[portal_key], PRODUCTION_TRANSITIONS[prod_key]


__all__ = [
    "EventSource",
    "InvalidTransitionError",
    "LifecycleEvent",
    "PORTAL_TRANSITIONS",
    "PRODUCTION_TRANSITIONS",
    "PortalStatus",
    "ProductionStatus",
    "TERMINAL_PORTAL_STATES",
    "TERMINAL_PRODUCTION_STATES",
    "apply_event",
    "is_valid",
]
