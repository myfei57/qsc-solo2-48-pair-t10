"""文件型持久化、审计留存与配置校验。"""

from __future__ import annotations

import copy
import os

import pytest

from glassmelt.config import (
    PlatformConfig,
    config_to_mapping,
    default_config,
    load_config,
)
from glassmelt.errors import ConfigError, GlassMeltError, StorageError
from glassmelt.store.journal import JsonlJournal
from glassmelt.store.jsonfile import JsonStore

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "glassmelt.yaml"
)


def test_json_store_round_trip(tmp_path):
    store = JsonStore(str(tmp_path / "docs"))
    assert store.exists("alpha") is False
    digest = store.write("alpha", {"value": 1})
    assert store.exists("alpha") is True
    assert store.read("alpha") == {"value": 1}
    assert store.digest("alpha") == digest
    assert store.read("missing") is None
    assert store.remove("alpha") is True
    assert store.remove("alpha") is False
    assert store.digest("alpha") == ""


def test_json_store_rejects_illegal_keys(tmp_path):
    store = JsonStore(str(tmp_path / "docs"))
    with pytest.raises(StorageError):
        store.path_for("")
    with pytest.raises(StorageError):
        store.path_for("nested/path")


def test_journal_rolls_over_segments(tmp_path):
    journal = JsonlJournal(str(tmp_path), "audit", segment_limit=2)
    for index in range(5):
        journal.append({"index": index})
    assert journal.segment_count() == 3
    assert [item["index"] for item in journal.read_all()] == [0, 1, 2, 3, 4]
    assert len(journal.read_segment(0)) == 2
    assert journal.read_segment(9) == []


def test_journal_reopens_with_existing_segments(tmp_path):
    root = str(tmp_path)
    first = JsonlJournal(root, "audit", segment_limit=2)
    first.append({"index": 0})
    first.append({"index": 1})
    second = JsonlJournal(root, "audit", segment_limit=2)
    second.append({"index": 2})
    assert second.segment_count() == 2
    assert [item["index"] for item in second.read_all()] == [0, 1, 2]


def test_audit_log_keeps_memory_and_disk_views(running):
    running.evaluate_anneal(running.config.temp.anneal_alarm_c + 5.0)
    alarms = running.audit.alarms()
    assert alarms
    assert any(item["severity"] == "alarm" for item in alarms)
    assert running.audit.entries(limit=5)
    tail = running.audit.tail(5)
    assert tail[0]["ts"] >= tail[-1]["ts"]
    summary = running.audit.summary()
    assert summary["total"] > 0
    assert summary["by_severity"]["alarm"] >= 1
    assert summary["segments"] >= 1
    assert running.audit.replay_from_journal()[0]["component"]
    assert running.audit.replay_segment(0)


def test_platform_error_is_recorded_and_typed(running):
    with pytest.raises(Exception):
        running.blow.normal(running.closer.closure())
    error = GlassMeltError("测试异常", component="mold")
    record = running.note_error(error)
    assert record["component"] == "mold"
    assert record["action"] == "error"
    assert record["severity"] == "warning"
    assert error.as_record()["type"] == "GlassMeltError"


def test_load_config_matches_defaults():
    loaded = load_config(CONFIG_PATH)
    default = default_config()
    assert loaded.name == default.name
    assert loaded.project_id == default.project_id
    assert loaded.pages == default.pages
    assert loaded.furnace == default.furnace
    assert loaded.burner == default.burner
    assert loaded.feeder == default.feeder
    assert loaded.lehr == default.lehr
    assert loaded.temp == default.temp


def test_config_mapping_round_trip():
    original = default_config()
    rebuilt = PlatformConfig.from_mapping(config_to_mapping(original))
    assert rebuilt == original
    assert rebuilt.pages == tuple(original.pages)


def test_config_rejects_missing_sections():
    mapping = config_to_mapping(default_config())
    broken = copy.deepcopy(mapping)
    del broken["limits"]["lehr"]
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(broken)
    missing_pages = copy.deepcopy(mapping)
    del missing_pages["pages"]
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(missing_pages)


def test_config_rejects_out_of_range_values():
    mapping = config_to_mapping(default_config())
    bad_temp = copy.deepcopy(mapping)
    bad_temp["limits"]["furnace"]["melt_temp_min_c"] = 1500.0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(bad_temp)
    bad_belt = copy.deepcopy(mapping)
    bad_belt["limits"]["lehr"]["belt_slow_m_per_min"] = 99.0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(bad_belt)
    bad_draft = copy.deepcopy(mapping)
    bad_draft["limits"]["furnace"]["draft_target_pa"] = 12.0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(bad_draft)
    bad_burner = copy.deepcopy(mapping)
    bad_burner["limits"]["burner"]["ignite_max_attempts"] = 0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(bad_burner)


def test_missing_config_file_is_reported(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.yaml"))
