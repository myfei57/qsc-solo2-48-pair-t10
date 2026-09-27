"""点火时序状态机。

点火顺序是「待助燃风 → 燃气投入 → 点火 → 燃烧」。正常点火与失焰重投走
同一套顺序：两条路径都必须先建立助燃风，再开燃气。只把正常路径堵住，重新
点火时炉膛里同样会先积一团可燃气。

失败处置分级，不再一失败就闩锁：判燃不过先关燃气、按连败次数拉长吹扫再
回到待机，等下一次触发重试；连败到上限才闩锁停住，等人工确认解除。正常
点火与失焰重投各算各的连败、各用各的上限——失焰重投失败不抵扣正常点火
的尝试次数，反之亦然；任一路径点着了，两路连败一起清零。
"""

from __future__ import annotations

import enum
import threading

from ..config import BurnerLimits
from ..errors import SequenceError
from ..eventbus import Event, EventBus
from ..furnace.gas import GasValve
from .air import CombustionAir

#: 失焰重投路径名；其余调用一律按正常点火路径记账。
RELIGHT_PATH = "reignite"


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
        IgnitionPhase.IDLE: (IgnitionPhase.AIR,),
        IgnitionPhase.AIR: (IgnitionPhase.GAS, IgnitionPhase.LATCHED),
        IgnitionPhase.GAS: (IgnitionPhase.IGNITE, IgnitionPhase.LATCHED),
        IgnitionPhase.IGNITE: (IgnitionPhase.FLAME, IgnitionPhase.PURGE, IgnitionPhase.LATCHED),
        IgnitionPhase.FLAME: (IgnitionPhase.PURGE, IgnitionPhase.IDLE),
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
        self._ignite_streak = 0
        self._relight_streak = 0
        self._ignite_failures = 0
        self._relight_failures = 0
        self._last_purge_flow_m3_h = 0.0
        self._lock = threading.RLock()

    @property
    def phase(self) -> IgnitionPhase:
        with self._lock:
            return self._phase

    def is_burning(self) -> bool:
        return self.phase is IgnitionPhase.FLAME

    def is_latched(self) -> bool:
        """闩锁停住、等待人工确认。"""

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

    def _max_attempts(self, path: str) -> int:
        """该路径允许连续失败的上限；到上限即闩锁等人工确认。"""

        if path == RELIGHT_PATH:
            return self._limits.relight_max_attempts
        return self._limits.ignite_max_attempts

    def _streak(self, path: str) -> int:
        with self._lock:
            return self._relight_streak if path == RELIGHT_PATH else self._ignite_streak

    def _record_failure(self, path: str) -> int:
        """给对应路径记一次连败，返回该路径当前连败数。"""

        with self._lock:
            if path == RELIGHT_PATH:
                self._relight_streak += 1
                self._relight_failures += 1
                return self._relight_streak
            self._ignite_streak += 1
            self._ignite_failures += 1
            return self._ignite_streak

    def _clear_streaks(self) -> None:
        """点着或人工确认后，两路连败一起清零。"""

        with self._lock:
            self._ignite_streak = 0
            self._relight_streak = 0

    def ignite(self, flame_temperature_c: float) -> dict:
        """正常点火：助燃风 → 燃气 → 点火 → 判燃。"""

        return self._run_sequence(flame_temperature_c, path="ignite")

    def reignite(self, flame_temperature_c: float, reason: str) -> dict:
        """失焰后重新点火：走与正常点火相同的助燃风前置顺序。

        失焰重投与反复点不着分开记账：重投失败只消耗重投自己的尝试上限。
        闩锁状态下禁止重投，必须人工确认解除闩锁后再来。
        """

        if self.is_latched():
            raise SequenceError("点火单元已闩锁，须人工确认解除后才能重投", component="burner")
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "reignite_requested",
                subject="igniter",
                severity="notice",
                reason=reason,
            )
        )
        if self.phase is IgnitionPhase.FLAME:
            self.purge(self._limits.air_min_flow_m3_h * 0.5)
        return self._run_sequence(flame_temperature_c, path=RELIGHT_PATH)

    def _run_sequence(self, flame_temperature_c: float, path: str) -> dict:
        with self._lock:
            self._starts += 1
        self._transition(IgnitionPhase.AIR)
        air_state = self._air.establish()
        self._transition(IgnitionPhase.GAS)
        gas_flow = self._gas.open(self._air.state())
        self._transition(IgnitionPhase.IGNITE)
        if not self._limits.flame_ok(flame_temperature_c):
            return self._handle_failure(path, air_state, gas_flow, flame_temperature_c)
        return self._handle_success(path, air_state, gas_flow, flame_temperature_c)

    def _handle_success(
        self, path: str, air_state, gas_flow: float, flame_temperature_c: float
    ) -> dict:
        """判燃通过：进燃烧相位，两路连败清零。"""

        prior_streak = self._streak(path)
        self._clear_streaks()
        self._transition(IgnitionPhase.FLAME)
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
                prior_streak=prior_streak,
            )
        )
        return {
            "path": path,
            "phase": self.phase.value,
            "air_established": air_state.established,
            "air_epoch": air_state.epoch,
            "gas_flow_m3_h": round(gas_flow, 2),
            "flame_ok": True,
            "prior_streak": prior_streak,
            "awaiting_manual_confirm": False,
        }

    def _handle_failure(
        self, path: str, air_state, gas_flow: float, flame_temperature_c: float
    ) -> dict:
        """判燃不过：未达上限拉长吹扫再试，到上限闩锁等人工确认。"""

        streak = self._record_failure(path)
        max_attempts = self._max_attempts(path)
        if streak < max_attempts:
            purge_flow = self._limits.retry_purge_flow_m3_h(streak)
            self.purge(purge_flow)
            with self._lock:
                self._last_purge_flow_m3_h = purge_flow
            self._bus.publish(
                Event.of(
                    "burner",
                    "burner",
                    "ignition_attempt_failed",
                    subject="igniter",
                    severity="warning",
                    path=path,
                    streak=streak,
                    max_attempts=max_attempts,
                    purge_flow_m3_h=round(purge_flow, 2),
                    flame_temperature_c=round(flame_temperature_c, 2),
                    air_epoch=air_state.epoch,
                )
            )
            return {
                "path": path,
                "phase": self.phase.value,
                "air_established": air_state.established,
                "air_epoch": air_state.epoch,
                "gas_flow_m3_h": round(gas_flow, 2),
                "flame_ok": False,
                "streak": streak,
                "max_attempts": max_attempts,
                "purge_flow_m3_h": round(purge_flow, 2),
                "awaiting_manual_confirm": False,
            }
        self._gas.close()
        self._transition(IgnitionPhase.LATCHED)
        with self._lock:
            self._latches += 1
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "ignition_failed",
                subject="igniter",
                severity="alarm",
                path=path,
                streak=streak,
                max_attempts=max_attempts,
                awaiting_manual_confirm=True,
                flame_temperature_c=round(flame_temperature_c, 2),
                air_epoch=air_state.epoch,
            )
        )
        return {
            "path": path,
            "phase": self.phase.value,
            "air_established": air_state.established,
            "air_epoch": air_state.epoch,
            "gas_flow_m3_h": round(gas_flow, 2),
            "flame_ok": False,
            "streak": streak,
            "max_attempts": max_attempts,
            "purge_flow_m3_h": 0.0,
            "awaiting_manual_confirm": True,
        }

    def purge(self, purge_flow_m3_h: float) -> dict:
        """吹扫：关燃气、按吹扫风量置换炉膛残余可燃气。"""

        self._gas.close()
        state = self._air.purge(purge_flow_m3_h)
        self._transition(IgnitionPhase.PURGE)
        self._transition(IgnitionPhase.IDLE)
        return {"phase": self.phase.value, "air": state.as_dict()}

    def release_latch(self, purge_flow_m3_h: float, reason: str = "人工确认解除闩锁") -> dict:
        """闩锁解除（人工确认）：先吹扫再回到待机，两路连败清零。"""

        with self._lock:
            if self._phase is not IgnitionPhase.LATCHED:
                raise SequenceError("点火单元不在闩锁状态", component="burner")
            cleared = (self._ignite_streak, self._relight_streak)
        result = self.purge(purge_flow_m3_h)
        self._clear_streaks()
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "ignition_latch_released",
                subject="igniter",
                severity="notice",
                reason=reason,
                cleared_ignite_streak=cleared[0],
                cleared_relight_streak=cleared[1],
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
                "ignite_streak": self._ignite_streak,
                "relight_streak": self._relight_streak,
                "ignite_failures": self._ignite_failures,
                "relight_failures": self._relight_failures,
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
                "escalation": {
                    "ignite_streak": counts["ignite_streak"],
                    "relight_streak": counts["relight_streak"],
                    "ignite_failures": counts["ignite_failures"],
                    "relight_failures": counts["relight_failures"],
                    "ignite_max_attempts": self._limits.ignite_max_attempts,
                    "relight_max_attempts": self._limits.relight_max_attempts,
                    "purge_escalation_pct": self._limits.purge_escalation_pct,
                    "last_purge_flow_m3_h": round(self._last_purge_flow_m3_h, 2),
                },
                "gas": self._gas.snapshot(),
                "air": self._air.snapshot(),
            }
