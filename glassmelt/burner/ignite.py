"""点火时序状态机。

点火顺序是「待助燃风 → 燃气投入 → 点火 → 燃烧」，任一步失败都进闩锁。
正常点火与失败后重新点火走的是同一套顺序：两条路径都必须先建立助燃风，
再开燃气。只把正常路径堵住，重新点火时炉膛里同样会先积一团可燃气。

点火失败按连续次数分级处置，不再一律闩锁等人来记次数：每次点不着先断
燃气，再按失败次数拉长吹扫、自动回到待机允许再试；连续失败到配置上限才
闩锁，停在那里等人工确认复位。失焰重投与反复点不着分开计数、分开上限：
重投超限同样闩锁等人工确认。两条线的计数与审计事件各自独立，判燃成功
或人工确认复位时才清零。
"""

from __future__ import annotations

import enum
import threading

from ..config import BurnerLimits
from ..errors import SequenceError
from ..eventbus import Event, EventBus
from ..furnace.gas import GasValve
from .air import CombustionAir


class IgnitionPhase(str, enum.Enum):
    """点火相位。"""

    IDLE = "idle"
    AIR = "air"
    GAS = "gas"
    IGNITE = "ignite"
    FLAME = "flame"
    PURGE = "purge"
    LATCHED = "latched"


class IgnitionUnit:
    """燃烧器点火单元。"""

    _ALLOWED = {
        IgnitionPhase.IDLE: (IgnitionPhase.AIR, IgnitionPhase.LATCHED),
        IgnitionPhase.AIR: (IgnitionPhase.GAS, IgnitionPhase.LATCHED),
        IgnitionPhase.GAS: (IgnitionPhase.IGNITE, IgnitionPhase.LATCHED),
        IgnitionPhase.IGNITE: (IgnitionPhase.FLAME, IgnitionPhase.PURGE, IgnitionPhase.LATCHED),
        IgnitionPhase.FLAME: (IgnitionPhase.PURGE, IgnitionPhase.IDLE, IgnitionPhase.LATCHED),
        IgnitionPhase.PURGE: (IgnitionPhase.IDLE, IgnitionPhase.AIR),
        IgnitionPhase.LATCHED: (IgnitionPhase.PURGE,),
    }

    def __init__(
        self,
        bus: EventBus,
        air: CombustionAir,
        gas: GasValve,
        limits: BurnerLimits,
    ) -> None:
        self._bus = bus
        self._air = air
        self._gas = gas
        self._limits = limits
        self._phase = IgnitionPhase.IDLE
        self._history: list[IgnitionPhase] = [IgnitionPhase.IDLE]
        self._starts = 0
        self._latches = 0
        self._ignition_failures = 0
        self._relight_attempts = 0
        self._lock = threading.RLock()

    @property
    def phase(self) -> IgnitionPhase:
        with self._lock:
            return self._phase

    def is_burning(self) -> bool:
        return self.phase is IgnitionPhase.FLAME

    def awaiting_manual_confirm(self) -> bool:
        """是否已闩锁停机、等待人工确认复位。"""

        return self.phase is IgnitionPhase.LATCHED

    def _transition(self, target: IgnitionPhase) -> IgnitionPhase:
        with self._lock:
            if target == self._phase:
                return self._phase
            if target not in self._ALLOWED[self._phase]:
                raise SequenceError(
                    f"点火不允许从 {self._phase.value} 迁移到 {target.value}",
                    component="burner",
                )
            self._phase = target
            self._history.append(target)
            return self._phase

    def ignite(self, flame_temperature_c: float) -> dict:
        """正常点火：助燃风 → 燃气 → 点火 → 判燃。"""

        return self._run_sequence(flame_temperature_c, path="ignite")

    def reignite(self, flame_temperature_c: float, reason: str) -> dict:
        """失焰重投：走与正常点火相同的助燃风前置顺序，重投次数单独计。"""

        if self.awaiting_manual_confirm():
            raise SequenceError("点火单元闩锁中，须人工确认复位后才能再投", component="burner")
        with self._lock:
            self._relight_attempts += 1
            attempt = self._relight_attempts
            phase = self._phase
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "reignite_requested",
                subject="igniter",
                severity="notice",
                reason=reason,
                relight_attempt=attempt,
            )
        )
        if attempt > self._limits.relight_max:
            return self._lockout(
                path="reignite",
                action="relight_lockout",
                flame_temperature_c=flame_temperature_c,
                base={},
                detail={
                    "reason": reason,
                    "relight_attempts": attempt,
                    "relight_max": self._limits.relight_max,
                },
            )
        if phase is IgnitionPhase.FLAME:
            self.purge(self._limits.air_min_flow_m3_h * 0.5)
        return self._run_sequence(flame_temperature_c, path="reignite")

    def _run_sequence(self, flame_temperature_c: float, path: str) -> dict:
        with self._lock:
            self._starts += 1
        self._transition(IgnitionPhase.AIR)
        air_state = self._air.establish()
        self._transition(IgnitionPhase.GAS)
        gas_flow = self._gas.open(self._air.state())
        self._transition(IgnitionPhase.IGNITE)
        base = {
            "path": path,
            "air_established": air_state.established,
            "air_epoch": air_state.epoch,
            "gas_flow_m3_h": round(gas_flow, 2),
        }
        if not self._limits.flame_ok(flame_temperature_c):
            return self._handle_failed_trial(path, flame_temperature_c, base)
        self._transition(IgnitionPhase.FLAME)
        with self._lock:
            self._ignition_failures = 0
            self._relight_attempts = 0
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "ignited",
                subject="igniter",
                severity="notice",
                path=path,
                gas_flow_m3_h=round(gas_flow, 2),
                flame_temperature_c=round(flame_temperature_c, 2),
                air_epoch=air_state.epoch,
            )
        )
        return {
            **base,
            "phase": self.phase.value,
            "disposition": "flame",
            "flame_ok": True,
            **self.counts(),
        }

    def _handle_failed_trial(self, path: str, flame_temperature_c: float, base: dict) -> dict:
        """点不着的分级处置：断燃气、拉长吹扫再试，超限闩锁等人工确认。"""

        with self._lock:
            self._ignition_failures += 1
            failures = self._ignition_failures
        detail = {
            "ignition_failures": failures,
            "retry_max": self._limits.ignition_retry_max,
        }
        if failures > self._limits.ignition_retry_max:
            return self._lockout(
                path=path,
                action="ignition_lockout",
                flame_temperature_c=flame_temperature_c,
                base=base,
                detail=detail,
            )
        purge_flow = self._limits.retry_purge_flow_m3_h(failures)
        self.purge(purge_flow)
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "ignition_retry_armed",
                subject="igniter",
                severity="warning",
                path=path,
                flame_temperature_c=round(flame_temperature_c, 2),
                purge_flow_m3_h=round(purge_flow, 2),
                retries_remaining=self._limits.ignition_retry_max - failures,
                **detail,
            )
        )
        return {
            **base,
            "phase": self.phase.value,
            "disposition": "retry",
            "flame_ok": False,
            "purge_flow_m3_h": round(purge_flow, 2),
            "retries_remaining": self._limits.ignition_retry_max - failures,
            **self.counts(),
        }

    def _lockout(
        self,
        path: str,
        action: str,
        flame_temperature_c: float,
        base: dict,
        detail: dict,
    ) -> dict:
        """闩锁停机：断燃气、发告警，停在闩锁态等人工确认。"""

        self._gas.close()
        self._transition(IgnitionPhase.LATCHED)
        with self._lock:
            self._latches += 1
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                action,
                subject="igniter",
                severity="alarm",
                path=path,
                flame_temperature_c=round(flame_temperature_c, 2),
                **detail,
            )
        )
        return {
            **base,
            "phase": self.phase.value,
            "disposition": "lockout",
            "manual_confirm_required": True,
            "flame_ok": False,
            **self.counts(),
        }

    def purge(self, purge_flow_m3_h: float) -> dict:
        """吹扫：关燃气、按吹扫风量置换炉膛残余可燃气。"""

        self._gas.close()
        state = self._air.purge(purge_flow_m3_h)
        self._transition(IgnitionPhase.PURGE)
        self._transition(IgnitionPhase.IDLE)
        return {"phase": self.phase.value, "air": state.as_dict()}

    def release_latch(self, purge_flow_m3_h: float, reason: str = "人工确认复位") -> dict:
        """闩锁解除：人工确认后先吹扫再回待机，分级计数一并清零。"""

        with self._lock:
            if self._phase is not IgnitionPhase.LATCHED:
                raise SequenceError("点火单元不在闩锁状态", component="burner")
            pending = {
                "ignition_failures": self._ignition_failures,
                "relight_attempts": self._relight_attempts,
            }
        result = self.purge(purge_flow_m3_h)
        with self._lock:
            self._ignition_failures = 0
            self._relight_attempts = 0
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "ignition_latch_released",
                subject="igniter",
                severity="notice",
                reason=reason,
                **pending,
            )
        )
        result["released_from_latch"] = True
        result["reason"] = reason
        return result

    def counts(self) -> dict:
        with self._lock:
            return {
                "starts": self._starts,
                "latches": self._latches,
                "ignition_failures": self._ignition_failures,
                "relight_attempts": self._relight_attempts,
            }

    def history(self) -> list[str]:
        with self._lock:
            return [phase.value for phase in self._history]

    def snapshot(self) -> dict:
        counts = self.counts()
        with self._lock:
            return {
                "phase": self._phase.value,
                "burning": self._phase is IgnitionPhase.FLAME,
                "awaiting_manual_confirm": self._phase is IgnitionPhase.LATCHED,
                "history": self.history(),
                "starts": counts["starts"],
                "latches": counts["latches"],
                "ignition_failures": counts["ignition_failures"],
                "relight_attempts": counts["relight_attempts"],
                "ignition_retry_max": self._limits.ignition_retry_max,
                "relight_max": self._limits.relight_max,
                "gas": self._gas.snapshot(),
                "air": self._air.snapshot(),
            }
