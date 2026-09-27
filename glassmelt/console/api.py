"""控制台 JSON 接口。

接口只做参数校验收敛与转发，业务判断全部留在组件里：状态来自 ``Platform``
的状态快照，趋势走趋势缓冲，投料／点火／开闸板／吹气／退火复位走平台动作。
控制台因此不会绕开任何一道路数门控。
"""

from __future__ import annotations

from ..app import CycleInputs, Platform
from ..errors import GlassMeltError

MAX_TREND_WINDOW_S = 86_400.0
MAX_AUDIT_LIMIT = 500


class ConsoleAPI:
    """把 HTTP 请求转成平台动作。"""

    def __init__(self, platform: Platform) -> None:
        self.platform = platform

    def state(self) -> dict:
        """工艺状态快照：组件状态、拓扑与命名空间。"""

        payload = self.platform.status()
        payload["topology"] = self.platform.topology_summary()
        payload["namespaces"] = self.platform.namespaces.describe()
        return payload

    def trend(self, series: str | None = None, window_s: float = 600.0) -> dict:
        """返回趋势窗口：采样点、均值与最新值。"""

        window = max(1.0, min(MAX_TREND_WINDOW_S, float(window_s)))
        names = [series] if series else self.platform.trend.series_names()
        unknown = [name for name in names if name not in self.platform.trend.series_names()]
        if unknown:
            raise ValueError(f"未知的趋势分量: {', '.join(unknown)}")
        end = self.platform.clock.now()
        start = end - window
        result: dict[str, dict] = {}
        for name in names:
            filled = self.platform.trend.fill_gaps(name)
            samples = self.platform.trend.window(name, start, end)
            latest = self.platform.trend.latest(name)
            result[name] = {
                "filled_points": filled,
                "samples": [sample.as_dict() for sample in samples],
                "average": round(self.platform.trend.average(name, start, end), 4),
                "latest": latest.as_dict() if latest else None,
            }
        return {"window_s": window, "start": round(start, 3), "end": round(end, 3), "series": result}

    def alarms(self) -> dict:
        alarms = self.platform.audit.alarms()
        return {"count": len(alarms), "alarms": alarms}

    def audit(self, limit: int = 100) -> dict:
        bounded = max(1, min(MAX_AUDIT_LIMIT, int(limit)))
        return {
            "entries": self.platform.audit.entries(bounded),
            "recent": self.platform.audit.tail(bounded // 4 + 1),
        }

    def journal(self, segment: int | None = None, limit: int = 200) -> dict:
        """读落盘日志；给段号时只读该段。"""

        if segment is None:
            return {"segment": None, "entries": self.platform.audit.replay_from_journal(limit)}
        return {"segment": segment, "entries": self.platform.audit.replay_segment(segment)}

    def readiness(self) -> dict:
        """投料、点火、开闸板、吹气与退火复位的前置条件。"""

        return self.platform.readiness_report()

    def start_batch(self, payload: dict) -> dict:
        path = str(payload.get("path", "start"))
        return self._guard(lambda: self.platform.start_charging(path=path))

    def ignite(self, payload: dict) -> dict:
        flame = float(payload.get("flame_temperature_c", 1420.0))
        path = str(payload.get("path", "ignite"))
        if path == "reignite":
            reason = str(payload.get("reason", "控制台触发的重新点火"))
            return self._guard(lambda: self.platform.reignite_burner(flame, reason))
        return self._guard(lambda: self.platform.ignite_burner(flame))

    def open_gate(self, payload: dict) -> dict:
        path = str(payload.get("path", "warm_up"))
        return self._guard(lambda: self.platform.open_forehearth_gate(path=path))

    def blow(self, payload: dict) -> dict:
        path = str(payload.get("path", "cycle"))
        return self._guard(lambda: self.platform.blow_mold(path=path))

    def reset_lehr(self, payload: dict) -> dict:
        reason = str(payload.get("reason", "控制台触发的退火复位"))
        return self._guard(lambda: self.platform.reset_lehr(reason=reason))

    def trip_burner(self, payload: dict) -> dict:
        zone = str(payload.get("zone", "zone-3"))
        reason = str(payload.get("reason", "控制台触发的窑温越限"))
        return self._guard(lambda: self.platform.trip_burner(zone, reason))

    def rebuild_zone_map(self, payload: dict) -> dict:
        raw = payload.get("assignments")
        if not isinstance(raw, dict):
            raw = {
                zone: f"burner-{index + 7}"
                for index, zone in enumerate(self.platform.zones.zones())
            }
        reason = str(payload.get("reason", "控制台触发的窑体大修"))
        return self._guard(lambda: self.platform.rebuild_zone_map(raw, reason))

    def clear_furnace(self, payload: dict) -> dict:
        reason = str(payload.get("reason", "控制台触发的出料清炉"))
        return self._guard(lambda: self.platform.clear_furnace(reason))

    def release_ignition_latch(self, payload: dict) -> dict:
        reason = str(payload.get("reason", "控制台人工确认复位"))
        return self._guard(lambda: self.platform.release_ignition_latch(reason))

    def calibrate(self, payload: dict) -> dict:
        offset = float(payload.get("offset_kg", 0.0))
        return self._guard(lambda: self.platform.recalibrate_scale(offset))

    def keep_alive(self, payload: dict) -> dict:
        """推进一个控制周期；缺省工况按 85% 熔制负荷取。"""

        inputs = CycleInputs(
            load=float(payload.get("load", 0.85)),
            pull_rate=float(payload.get("pull_rate", 0.9)),
            gob_mass_g=float(payload.get("gob_mass_g", self.platform.config.feeder.gob_mass_target_g)),
            dt=float(payload.get("dt", 1.0)),
        )
        return self._guard(lambda: self.platform.cycle(inputs))

    def start(self) -> dict:
        return self._guard(lambda: self.platform.start())

    def stop(self) -> dict:
        return self._guard(lambda: self.platform.stop())

    def _guard(self, action) -> dict:
        try:
            return {"ok": True, "result": action()}
        except GlassMeltError as error:
            return {"ok": False, "error": self.platform.note_error(error)}
