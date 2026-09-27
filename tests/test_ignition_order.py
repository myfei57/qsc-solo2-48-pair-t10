"""点火顺序：助燃风必须先于燃气，两条点火路径都要复核；失败处置分级。"""

from __future__ import annotations

import pytest

from glassmelt.app import CycleInputs
from glassmelt.errors import InterlockError, SequenceError

LOW_FLAME_C = 120.0


def test_gas_valve_refuses_to_open_without_air(platform):
    with pytest.raises(InterlockError):
        platform.gas.open(platform.air.state())
    assert platform.gas.is_open is False


def test_ignition_establishes_air_before_gas(platform):
    result = platform.ignite_burner(platform.furnace_temp.target_c + 10.0)
    assert result["flame_ok"] is True
    assert result["air_established"] is True
    assert platform.air.flow_m3_h >= platform.config.burner.air_min_flow_m3_h
    assert platform.gas.flow_m3_h > 0
    assert platform.igniter.is_burning() is True


def test_stale_air_state_is_not_reused_after_close(platform):
    platform.ignite_burner(platform.furnace_temp.target_c + 10.0)
    assert platform.air.state().established is True
    platform.air.close()
    assert platform.air.state().established is False
    with pytest.raises(InterlockError):
        platform.gas.open(platform.air.state())


def test_purge_invalidates_air_state(platform):
    platform.ignite_burner(platform.furnace_temp.target_c + 10.0)
    state = platform.air.purge(200.0)
    assert state.established is False
    assert state.epoch > 0
    with pytest.raises(InterlockError):
        platform.gas.open(platform.air.state())


def test_failed_ignition_escalates_purge_then_latches(platform):
    limits = platform.config.burner
    first = platform.ignite_burner(LOW_FLAME_C)
    assert first["flame_ok"] is False
    assert first["streak"] == 1
    assert first["awaiting_manual_confirm"] is False
    assert platform.igniter.phase.value == "idle"
    assert first["purge_flow_m3_h"] == pytest.approx(limits.air_min_flow_m3_h * 0.9)

    second = platform.ignite_burner(LOW_FLAME_C)
    assert second["streak"] == 2
    assert second["purge_flow_m3_h"] > first["purge_flow_m3_h"]
    assert platform.igniter.phase.value == "idle"
    assert platform.igniter.counts()["latches"] == 0

    third = platform.ignite_burner(LOW_FLAME_C)
    assert third["streak"] == limits.ignite_max_attempts
    assert third["awaiting_manual_confirm"] is True
    assert platform.igniter.is_latched() is True
    assert platform.igniter.counts()["latches"] == 1
    assert platform.gas.is_open is False


def test_retry_purge_flow_grows_with_each_failure(platform):
    seen = []
    unsubscribe = platform.bus.subscribe("burner", lambda event: seen.append(event))
    platform.ignite_burner(LOW_FLAME_C)
    platform.ignite_burner(LOW_FLAME_C)
    unsubscribe()
    purges = [
        event.payload["flow_m3_h"] for event in seen if event.action == "air_purged"
    ]
    assert len(purges) == 2
    assert purges[1] > purges[0]


def test_latched_igniter_waits_for_manual_release(platform):
    for _ in range(platform.config.burner.ignite_max_attempts):
        platform.ignite_burner(LOW_FLAME_C)
    assert platform.igniter.is_latched() is True
    with pytest.raises(SequenceError):
        platform.igniter.ignite(1400.0)
    with pytest.raises(SequenceError):
        platform.igniter.reignite(1400.0, "失焰")

    released = platform.release_ignition_latch()
    assert released["released_from_latch"] is True
    assert platform.igniter.phase.value == "idle"
    assert platform.air.is_established() is False
    snapshot = platform.igniter.snapshot()
    assert snapshot["awaiting_manual_confirm"] is False
    assert snapshot["escalation"]["ignite_streak"] == 0

    result = platform.ignite_burner(platform.furnace_temp.target_c + 10.0)
    assert result["flame_ok"] is True
    assert platform.igniter.is_burning() is True


def test_successful_ignition_clears_failure_streak(platform):
    platform.ignite_burner(LOW_FLAME_C)
    assert platform.igniter.counts()["ignite_streak"] == 1
    ok = platform.ignite_burner(platform.furnace_temp.target_c + 10.0)
    assert ok["flame_ok"] is True
    assert ok["prior_streak"] == 1
    assert platform.igniter.counts()["ignite_streak"] == 0

    platform.igniter.purge(200.0)
    again = platform.ignite_burner(LOW_FLAME_C)
    assert again["streak"] == 1
    assert again["awaiting_manual_confirm"] is False


def test_relight_failures_are_tracked_separately(platform):
    platform.ignite_burner(platform.furnace_temp.target_c + 10.0)
    platform.air.close()
    platform.gas.close()
    first = platform.reignite_burner(LOW_FLAME_C, "失焰")
    assert first["path"] == "reignite"
    assert first["streak"] == 1
    assert first["awaiting_manual_confirm"] is False
    counts = platform.igniter.counts()
    assert counts["relight_streak"] == 1
    assert counts["ignite_streak"] == 0

    second = platform.reignite_burner(LOW_FLAME_C, "失焰")
    assert second["awaiting_manual_confirm"] is True
    assert platform.igniter.is_latched() is True
    counts = platform.igniter.counts()
    assert counts["relight_failures"] == 2
    assert counts["ignite_failures"] == 0
    assert counts["latches"] == 1


def test_ignite_and_relight_streaks_do_not_mix(platform):
    platform.ignite_burner(LOW_FLAME_C)
    platform.ignite_burner(LOW_FLAME_C)
    relight = platform.reignite_burner(LOW_FLAME_C, "失焰")
    assert relight["streak"] == 1
    assert relight["awaiting_manual_confirm"] is False
    counts = platform.igniter.counts()
    assert counts["ignite_streak"] == 2
    assert counts["relight_streak"] == 1

    ok = platform.reignite_burner(platform.furnace_temp.target_c + 10.0, "失焰")
    assert ok["flame_ok"] is True
    counts = platform.igniter.counts()
    assert counts["ignite_streak"] == 0
    assert counts["relight_streak"] == 0


def test_control_cycle_holds_when_igniter_latched(running):
    running.reignite_burner(LOW_FLAME_C, "失焰")
    running.reignite_burner(LOW_FLAME_C, "失焰")
    assert running.igniter.is_latched() is True
    starts_before = running.igniter.counts()["starts"]
    running.cycle(CycleInputs(load=0.8, pull_rate=0.9, gob_mass_g=186.0, dt=1.0))
    assert running.igniter.counts()["starts"] == starts_before
    assert running.igniter.is_latched() is True


def test_escalation_events_reach_audit(platform):
    for _ in range(platform.config.burner.ignite_max_attempts):
        platform.ignite_burner(LOW_FLAME_C)
    actions = [item["action"] for item in platform.audit.alarms()]
    assert "ignition_attempt_failed" in actions
    assert "ignition_failed" in actions


def test_reignition_path_also_requires_air_first(platform):
    platform.ignite_burner(platform.furnace_temp.target_c + 10.0)
    platform.air.close()
    platform.gas.close()
    result = platform.reignite_burner(platform.furnace_temp.target_c + 15.0, "失焰")
    assert result["path"] == "reignite"
    assert result["air_established"] is True
    assert platform.air.state().epoch >= 1
    assert platform.igniter.is_burning() is True


def test_zone_map_rebuild_changes_the_current_revision(platform):
    assert platform.zones.revision == 1
    assert platform.zones.is_current(1) is True
    assignments = {
        zone: f"burner-{index + 9}" for index, zone in enumerate(platform.zones.zones())
    }
    revision = platform.zones.rebuild(assignments, "窑体大修")
    assert revision == 2
    assert platform.zones.is_current(1) is False
    assert platform.zones.resolve("zone-1").burner_id == "burner-9"
    with pytest.raises(InterlockError):
        platform.zones.rebuild({"zone-1": "burner-x"}, "缺分区")
