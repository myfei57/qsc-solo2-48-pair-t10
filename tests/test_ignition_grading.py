"""点火失败分级处置：拉长吹扫再试、超限闩锁、失焰重投单独计数。"""

from __future__ import annotations

import pytest

from glassmelt.app import CycleInputs
from glassmelt.console.api import ConsoleAPI
from glassmelt.errors import SequenceError

COLD_FLAME_C = 120.0


def _drive_to_lockout(platform) -> dict:
    """连续点不着直到超限闩锁，返回闩锁那次的结果。"""

    retry_max = platform.config.burner.ignition_retry_max
    for _ in range(retry_max):
        platform.ignite_burner(COLD_FLAME_C)
    return platform.ignite_burner(COLD_FLAME_C)


def test_consecutive_failures_extend_the_purge(platform):
    limits = platform.config.burner
    base = limits.air_min_flow_m3_h * 0.9
    first = platform.ignite_burner(COLD_FLAME_C)
    assert first["disposition"] == "retry"
    assert first["phase"] == "idle"
    assert first["purge_flow_m3_h"] == pytest.approx(base)
    assert first["retries_remaining"] == limits.ignition_retry_max - 1
    second = platform.ignite_burner(COLD_FLAME_C)
    assert second["purge_flow_m3_h"] == pytest.approx(base * limits.purge_extend_factor)
    third = platform.ignite_burner(COLD_FLAME_C)
    assert third["purge_flow_m3_h"] == pytest.approx(base * limits.purge_extend_factor**2)
    assert third["retries_remaining"] == 0
    assert platform.igniter.counts()["ignition_failures"] == 3


def test_lockout_halts_until_manual_confirmation(platform):
    locked = _drive_to_lockout(platform)
    assert locked["disposition"] == "lockout"
    assert locked["manual_confirm_required"] is True
    assert platform.gas.is_open is False
    assert platform.igniter.awaiting_manual_confirm() is True
    with pytest.raises(SequenceError):
        platform.ignite_burner(platform.furnace_temp.target_c + 10.0)
    released = platform.release_ignition_latch("测试人工确认")
    assert released["released_from_latch"] is True
    assert released["reason"] == "测试人工确认"
    counts = platform.igniter.counts()
    assert counts["ignition_failures"] == 0
    assert counts["relight_attempts"] == 0
    ok = platform.ignite_burner(platform.furnace_temp.target_c + 10.0)
    assert ok["flame_ok"] is True


def test_proven_flame_resets_grading_counters(platform):
    platform.ignite_burner(COLD_FLAME_C)
    assert platform.igniter.counts()["ignition_failures"] == 1
    platform.ignite_burner(platform.furnace_temp.target_c + 10.0)
    counts = platform.igniter.counts()
    assert counts["ignition_failures"] == 0
    assert counts["relight_attempts"] == 0
    platform.igniter.purge(100.0)
    again = platform.ignite_burner(COLD_FLAME_C)
    assert again["disposition"] == "retry"
    assert again["ignition_failures"] == 1
    base = platform.config.burner.air_min_flow_m3_h * 0.9
    assert again["purge_flow_m3_h"] == pytest.approx(base)


def test_relight_recycles_are_counted_separately(platform):
    platform.ignite_burner(platform.furnace_temp.target_c + 10.0)
    platform.air.close()
    platform.gas.close()
    first = platform.reignite_burner(COLD_FLAME_C, "失焰")
    assert first["disposition"] == "retry"
    assert first["relight_attempts"] == 1
    assert first["ignition_failures"] == 1
    second = platform.reignite_burner(COLD_FLAME_C, "再次失焰")
    assert second["relight_attempts"] == 2
    assert second["ignition_failures"] == 2
    third = platform.reignite_burner(COLD_FLAME_C, "第三次失焰")
    assert third["disposition"] == "lockout"
    assert third["relight_attempts"] == 3
    assert third["ignition_failures"] == 2
    assert third["starts"] == second["starts"]
    assert platform.igniter.phase.value == "latched"
    actions = [event.action for event in platform.bus.history("burner")]
    assert "relight_lockout" in actions
    assert "ignition_lockout" not in actions


def test_reignite_is_refused_while_latched(platform):
    _drive_to_lockout(platform)
    assert platform.igniter.awaiting_manual_confirm() is True
    with pytest.raises(SequenceError):
        platform.reignite_burner(platform.furnace_temp.target_c + 10.0, "失焰")


def test_release_requires_a_latched_unit(platform):
    with pytest.raises(SequenceError):
        platform.release_ignition_latch()


def test_control_cycle_stops_relighting_once_latched(running):
    running.igniter.purge(100.0)
    _drive_to_lockout(running)
    assert running.igniter.awaiting_manual_confirm() is True
    running.cycle(CycleInputs(load=0.8, pull_rate=1.0, gob_mass_g=186.0, dt=1.0))
    assert running.igniter.phase.value == "latched"
    assert running.igniter.counts()["relight_attempts"] == 0


def test_grading_events_are_audited_with_counts(platform):
    retry_max = platform.config.burner.ignition_retry_max
    _drive_to_lockout(platform)
    platform.release_ignition_latch("测试复位")
    events = platform.bus.history("burner")
    actions = [event.action for event in events]
    assert actions.count("ignition_retry_armed") == retry_max
    assert "ignition_lockout" in actions
    released = [event for event in events if event.action == "ignition_latch_released"]
    assert released
    assert released[0].payload["reason"] == "测试复位"
    assert released[0].payload["ignition_failures"] == retry_max + 1


def test_console_release_passes_manual_confirmation_reason(platform):
    api = ConsoleAPI(platform)
    _drive_to_lockout(platform)
    result = api.release_ignition_latch({"reason": "接口人工确认"})
    assert result["ok"] is True
    assert result["result"]["reason"] == "接口人工确认"
