"""点火顺序：助燃风必须先于燃气，两条点火路径都要复核。"""

from __future__ import annotations

import pytest

from glassmelt.errors import InterlockError, SequenceError


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


def test_failed_ignition_latches_and_can_be_released(platform):
    retry_max = platform.config.burner.ignition_retry_max
    for _ in range(retry_max):
        retried = platform.ignite_burner(120.0)
        assert retried["disposition"] == "retry"
    result = platform.ignite_burner(120.0)
    assert result["flame_ok"] is False
    assert result["disposition"] == "lockout"
    assert platform.igniter.phase.value == "latched"
    assert platform.igniter.snapshot()["latches"] == 1
    with pytest.raises(SequenceError):
        platform.igniter.ignite(1400.0)
    released = platform.release_ignition_latch()
    assert released["released_from_latch"] is True
    assert platform.igniter.phase.value == "idle"
    assert platform.air.is_established() is False


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
