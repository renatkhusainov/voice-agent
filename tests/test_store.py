"""app/agent/store.py: Redis-backed DialogState get/set/delete. Offline —
fakeredis.FakeRedis() stands in for a real Redis, injected via the `client`
kwarg every function here accepts for exactly this reason.
"""

from datetime import datetime, timezone

import fakeredis
import pytest

from app.agent import store
from app.agent.state import BookingSlots, DialogIntent, DialogState


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


def test_get_returns_none_for_an_unknown_key(redis):
    assert store.get_state("no-such-session", client=redis) is None


def test_set_then_get_round_trips_a_plain_state(redis):
    state = DialogState(intent=DialogIntent.check_availability, turn_count=3)

    store.set_state("s1", state, client=redis)
    loaded = store.get_state("s1", client=redis)

    assert loaded == state


def test_round_trip_preserves_nested_slots_and_datetimes(redis):
    state = DialogState(
        intent=DialogIntent.book_appointment,
        slots=BookingSlots(
            practice_id=1, caller_name="Dana Lee", callback_number="+18135550142",
            service="cleaning", requested_slot=datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc),
        ),
        pending_confirmation=True,
        proposed_at_turn=2,
        turn_count=3,
    )

    store.set_state("s1", state, client=redis)
    loaded = store.get_state("s1", client=redis)

    assert loaded == state
    assert loaded.slots.requested_slot == state.slots.requested_slot
    assert loaded.slots.requested_slot.tzinfo is not None  # survives as timezone-aware


def test_different_keys_do_not_collide(redis):
    a = DialogState(turn_count=1)
    b = DialogState(turn_count=99)

    store.set_state("session-a", a, client=redis)
    store.set_state("session-b", b, client=redis)

    assert store.get_state("session-a", client=redis).turn_count == 1
    assert store.get_state("session-b", client=redis).turn_count == 99


def test_set_overwrites_the_previous_value_for_the_same_key(redis):
    store.set_state("s1", DialogState(turn_count=1), client=redis)
    store.set_state("s1", DialogState(turn_count=2), client=redis)

    assert store.get_state("s1", client=redis).turn_count == 2


def test_delete_removes_the_state(redis):
    store.set_state("s1", DialogState(), client=redis)

    store.delete_state("s1", client=redis)

    assert store.get_state("s1", client=redis) is None


def test_state_is_stored_with_a_ttl(redis):
    store.set_state("s1", DialogState(), client=redis)

    ttl = redis.ttl(store.KEY_PREFIX + "s1")

    assert 0 < ttl <= store.TTL_SECONDS


def test_key_is_namespaced_with_the_prefix(redis):
    store.set_state("my-session", DialogState(), client=redis)

    assert redis.exists(store.KEY_PREFIX + "my-session")
    assert not redis.exists("my-session")  # not stored under the bare key


def test_default_redis_url_is_localhost_and_compose_overrides_it_for_the_container():
    # A live call run with uvicorn on the host went silent because the old
    # default, redis:6379, only resolves inside Docker.
    from pathlib import Path

    from app.config import Settings

    assert Settings.model_fields["redis_url"].default == "redis://localhost:6379/0"
    compose = (Path(__file__).resolve().parent.parent / "docker-compose.yaml").read_text()
    assert "REDIS_URL: redis://redis:6379/0" in compose
