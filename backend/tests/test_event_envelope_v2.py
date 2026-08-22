from __future__ import annotations

import pytest

from backend.domain.agents import AgentEvent
from backend.domain.agents.events import (
    DEFAULT_EVENT_PRODUCER,
    EVENT_ENVELOPE_SCHEMA_VERSION,
    EventEnvelope,
    EventEnvelopeV2,
)


def _legacy_event(*, payload: dict[str, object] | None = None) -> AgentEvent:
    return AgentEvent(
        "message.delta",
        "req-1",
        "thread-1",
        7,
        payload or {"content": "hello"},
        "2026-07-11T00:00:00+00:00",
    )


def test_agent_event_legacy_constructor_and_sse_wire_payload_are_unchanged():
    event = _legacy_event()

    assert event.to_sse_payload() == {
        "type": "message.delta",
        "payload": {"content": "hello"},
        "request_id": "req-1",
        "thread_id": "thread-1",
        "sequence": 7,
        "created_at": "2026-07-11T00:00:00+00:00",
    }


def test_agent_event_promotes_to_complete_v2_envelope_with_stable_identity():
    event = _legacy_event(payload={"content": "hello", "unit_id": "unit-1"})

    envelope = event.to_envelope()
    retried_envelope = event.to_envelope()

    assert envelope.to_dict() == {
        "schema_version": EVENT_ENVELOPE_SCHEMA_VERSION,
        "event_id": envelope.event_id,
        "type": "message.delta",
        "request_id": "req-1",
        "thread_id": "thread-1",
        "sequence": 7,
        "correlation_id": "req-1",
        "causation_id": None,
        "producer": DEFAULT_EVENT_PRODUCER,
        "unit_id": "unit-1",
        "idempotency_key": envelope.event_id,
        "payload": {"content": "hello", "unit_id": "unit-1"},
        "created_at": "2026-07-11T00:00:00+00:00",
    }
    assert envelope.event_id.startswith("evt_")
    assert retried_envelope.event_id == envelope.event_id
    assert retried_envelope.idempotency_key == envelope.idempotency_key


def test_v2_envelope_supports_explicit_lineage_and_round_trip():
    event = _legacy_event()
    envelope = event.to_envelope(
        event_id="evt-explicit",
        correlation_id="corr-1",
        causation_id="evt-parent",
        producer="coordinator",
        unit_id="unit-explicit",
        idempotency_key="delivery-1",
    )

    restored = EventEnvelopeV2.from_dict(envelope.to_dict())

    assert EventEnvelope is EventEnvelopeV2
    assert restored == envelope
    assert restored.correlation_id == "corr-1"
    assert restored.causation_id == "evt-parent"
    assert restored.producer == "coordinator"
    assert restored.unit_id == "unit-explicit"
    assert restored.idempotency_key == "delivery-1"
    assert restored.to_agent_event().to_sse_payload() == event.to_sse_payload()


def test_v2_envelope_defensively_copies_payload():
    payload = {"content": "before"}
    envelope = _legacy_event(payload=payload).to_envelope()

    payload["content"] = "after"
    serialized = envelope.to_dict()
    serialized["payload"]["content"] = "serialized-change"

    assert envelope.payload == {"content": "before"}


def test_v2_envelope_rejects_wrong_schema_and_non_mapping_payload():
    raw = _legacy_event().to_envelope().to_dict()
    raw["schema_version"] = "1.0"

    with pytest.raises(ValueError, match="requires schema_version"):
        EventEnvelopeV2.from_dict(raw)

    raw["schema_version"] = EVENT_ENVELOPE_SCHEMA_VERSION
    raw["payload"] = "not-a-mapping"
    with pytest.raises(TypeError, match="payload must be a mapping"):
        EventEnvelopeV2.from_dict(raw)


def test_task_runtime_unit_identity_is_carried_into_v2_envelope():
    event = AgentEvent.from_runtime_payload(
        {
            "type": "task_started",
            "task_id": "task-1",
            "subagent_type": "research",
        },
        request_id="req-1",
        thread_id="thread-1",
        sequence=8,
    )

    envelope = event.to_envelope()

    assert event.type == "subagent.started"
    assert envelope.unit_id == "task-1"
    assert envelope.payload["agent_type"] == "research"
