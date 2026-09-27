"""平台装配与控制循环。

``Platform`` 把各组件按真实调用链接起来，并暴露四件事：启动
顺序、停炉顺序、控制循环和状态快照。控制循环每个周期做六件事：按负荷与
拉引量调整液面并按液面门控投料、按投料秤基线折算配比、维持燃烧与分区、
按稳温结论决定闸板、走一遍成型循环、最后按退火曲线结算网带与加热功率。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

from .audit.log import AuditLog
from .audit.record import AuditRecord
from .batch.charge import Charger
from .batch.scale import BatchScale
from .burner.air import CombustionAir
from .burner.draft import air_closed_before_draft_stop, close_combustion_air
from .burner.ignite import IgnitionUnit
from .burner.zone import ZoneMap
from .clock import Clock, SystemClock
from .config import PlatformConfig, default_config
from .errors import GlassMeltError, SequenceError
from .eventbus import Event, EventBus
from .feeder.cut import Shear
from .feeder.cycle import FormingCycle
from .feeder.gob import GobFormer
from .forehearth.gate import ForehearthGate
from .forehearth.steady import ForehearthSteady
from .furnace.draft import DraftFan
from .furnace.gas import GasValve
from .furnace.level import GlassLevel
from .furnace.mix import MixPlanner
from .furnace.temp import FurnaceTemperature
from .lehr.anneal import AnnealZones
from .lehr.belt import BeltDrive
from .lehr.heater import HeaterBank
from .mold.blow import BlowStation
from .mold.close import MoldCloser
from .mold.fill import MoldFill
from .ns.registry import NamespaceRegistry
from .ns.topology import Topology, build_default_topology
from .store.journal import JsonlJournal
from .store.jsonfile import JsonStore
from .temp.alarm import AnnealAlarm
from .temp.curve import AnnealCurve
from .temp.latch import HeaterLatch
from .temp.stable import StabilityMonitor
from .temp.trip import TripInterlock
from .telemetry import TrendBuffer

DEFAULT_UNIT = "tank1"

#: 启动顺序的硬约束：(先发生的步骤, 必须后发生的步骤, 说明)
START_ORDER_RULES: tuple[tuple[str, str, str], ...] = (
    ("draft_start", "ignite", "炉膛负压必须先于点火建立"),
    ("air_establish", "ignite", "助燃风必须先于燃气投入"),
    ("scale_baseline", "mix_plan", "投料秤基线必须先于配比折算"),
    ("charge", "mix_plan", "投料后才有本批配比"),
    ("stability_warm", "gate_open", "料道稳温必须先于开闸板"),
    ("gate_open", "gob_form", "开闸板后料滴才流出"),
    ("gob_form", "shear", "料滴成型必须先于剪切"),
    ("shear", "mold_cycle", "剪切后才有可灌模的料滴"),
)

#: 停炉顺序的硬约束。
STOP_ORDER_RULES: tuple[tuple[str, str, str], ...] = (
    ("gate_close", "air_close", "停炉先关闸板"),
    ("air_close", "draft_stop", "助燃风先关、排烟风机后停"),
)

#: 步骤归属的组件名，用于对照链路拓扑。
STEP_COMPONENT: dict[str, str] = {
    "draft_start": "furnace",
    "air_establish": "burner",
    "ignite": "burner",
    "scale_baseline": "batch",
    "charge": "batch",
    "mix_plan": "furnace",
    "stability_warm": "temp",
    "gate_open": "forehearth",
    "gob_form": "feeder",
    "shear": "feeder",
    "mold_cycle": "mold",
    "anneal_start": "lehr",
}

#: 拓扑里必须存在的上游／下游关系。
TOPOLOGY_RULES: tuple[tuple[str, str], ...] = (
    ("batch", "furnace"),
    ("burner", "furnace"),
    ("furnace", "forehearth"),
    ("feeder", "mold"),
    ("mold", "lehr"),
    ("temp", "lehr"),
)


@dataclass(frozen=True)
class CycleInputs:
    """一个控制周期的工况输入。"""

    load: float
    pull_rate: float
    gob_mass_g: float
    dt: float

    def clamped(self) -> "CycleInputs":
        return CycleInputs(
            load=max(0.0, min(1.0, float(self.load))),
            pull_rate=max(0.0, min(2.0, float(self.pull_rate))),
            gob_mass_g=max(1.0, float(self.gob_mass_g)),
            dt=max(0.001, float(self.dt)),
        )


class Platform:
    """玻璃熔窑成型与退火控制平台。"""

    def __init__(
        self,
        config: PlatformConfig | None = None,
        clock: Clock | None = None,
        unit: str = DEFAULT_UNIT,
    ) -> None:
        self.config = config or default_config()
        self.clock = clock or SystemClock()
        self.bus = EventBus()

        store = JsonStore(self.config.storage_root)
        self.store = store
        self.audit = AuditLog(
            self.clock, self.bus, JsonlJournal(store.root, "audit", segment_limit=512)
        )
        self.namespaces = NamespaceRegistry(unit)
        self.topology: Topology = build_default_topology()
        self.trend = TrendBuffer(period_s=5.0)

        self.level = GlassLevel(self.clock, self.bus, self.config.furnace)
        self.furnace_temp = FurnaceTemperature(self.bus, self.config.furnace)
        self.mix = MixPlanner(self.bus, self.config.batch)
        self.draft = DraftFan(self.bus, self.config.furnace)
        self.gas = GasValve(self.bus, self.config.burner.gas_max_m3_h)
        self.air = CombustionAir(self.clock, self.bus, self.config.burner)
        self.igniter = IgnitionUnit(self.bus, self.air, self.gas, self.config.burner)
        self.zones = ZoneMap(self.bus, self.config.burner)

        self.scale = BatchScale(self.bus, store, self.config.batch)
        self.charger = Charger(self.bus, self.config.batch)

        self.stability = StabilityMonitor(self.clock, self.bus, self.config.forehearth)
        self.forehearth_temp = ForehearthSteady(self.bus, self.config.forehearth)
        self.gate = ForehearthGate(self.bus, self.config.forehearth)

        self.alarm = AnnealAlarm(self.clock, self.bus, self.config.temp)
        self.latch = HeaterLatch(self.clock, self.bus)
        self.curve = AnnealCurve(self.bus, self.config.temp, self.config.lehr)
        self.trip = TripInterlock(self.clock, self.bus, self.zones)

        self.gob = GobFormer(self.clock, self.bus, self.config.feeder)
        self.shear = Shear(self.bus, self.config.feeder)
        self.forming = FormingCycle(self.bus)
        self.closer = MoldCloser(self.clock, self.bus, self.config.mold)
        self.blow = BlowStation(self.bus, self.config.mold)
        self.filler = MoldFill(self.bus, self.config.feeder)

        self.belt = BeltDrive(self.clock, self.bus, self.config.lehr)
        self.heater = HeaterBank(self.clock, self.bus, self.config.lehr)
        self.anneal = AnnealZones(self.bus, self.config.lehr)

        self._cycles = 0
        self._elapsed_s = 0.0
        self._started = False
        self._startup_steps: list[str] = []
        self._shutdown_steps: list[str] = []
        self._lock = threading.RLock()
        self._register_namespaces()

    def _register_namespaces(self) -> None:
        """登记现场设备命名空间（控制台与审计共用同一套名字）。"""

        for area, device, kind in (
            ("batch", "charger", "batch-charger"),
            ("batch", "scale", "batch-scale"),
            ("furnace", "level", "glass-level-probe"),
            ("furnace", "melt_temp", "thermocouple"),
            ("furnace", "draft", "draft-fan"),
            ("burner", "air", "combustion-air-fan"),
            ("burner", "gas", "gas-control-valve"),
            ("burner", "zone", "zone-burner"),
            ("forehearth", "steady", "forehearth-heater"),
            ("forehearth", "gate", "forehearth-gate"),
            ("temp", "anneal", "anneal-thermocouple"),
            ("temp", "latch", "anneal-protection-latch"),
            ("feeder", "gob", "gob-former"),
            ("feeder", "shear", "shear-blade"),
            ("mold", "closer", "mold-closer"),
            ("mold", "blow", "blow-station"),
            ("mold", "fill", "mold-cavity"),
            ("lehr", "belt", "lehr-belt"),
            ("lehr", "heater", "lehr-heater-bank"),
        ):
            self.namespaces.register(area, device, kind)

    def _note(self, steps: list[str], step: str, **detail: object) -> None:
        steps.append(step)
        self.bus.publish(
            Event.of(
                "console",
                STEP_COMPONENT.get(step, "console"),
                "sequence_step",
                subject=step,
                step_index=len(steps),
                **detail,
            )
        )

    def _wait(self, seconds: float) -> float:
        """推进工艺时间；可推进时钟直接推进，系统时钟按真实时间等待。"""

        if seconds > 0:
            self.clock.sleep(seconds)
        return self.clock.now()

    def start(self) -> dict:
        """执行启动顺序：投料 → 熔化 → 供料道 → 成型 → 退火。"""

        with self._lock:
            self._startup_steps = []
            steps = self._startup_steps
            pressure = self.draft.start()
            self._note(steps, "draft_start", pressure_pa=round(pressure, 3))

            air_state = self.air.establish()
            self._note(steps, "air_establish", flow_m3_h=round(air_state.flow_m3_h, 2))

            if self.igniter.is_burning():
                ignition = {"phase": self.igniter.phase.value}
            else:
                ignition = self.igniter.ignite(self.furnace_temp.target_c + 20.0)
            self.furnace_temp.observe(self.furnace_temp.target_c + 12.0)
            self._note(steps, "ignite", phase=ignition["phase"])

            if not self.scale.restore():
                self.scale.recalibrate(0.0, self.clock.now())
            self.scale.require_durable()
            self._note(steps, "scale_baseline", epoch=self.scale.epoch)

            self.level.observe(self.config.furnace.level_target_mm - 24.0)
            charge = self.charger.start(self.level)
            self.level.drift(18.0)
            self._note(steps, "charge", batch_kg=round(charge.batch_kg, 3))

            plan = self.mix.plan(self.scale.baseline())
            self._note(
                steps,
                "mix_plan",
                baseline_epoch=plan.baseline_epoch,
                deviation_pct=round(plan.deviation_pct, 4),
            )

            self.forehearth_temp.observe(self.forehearth_temp.setpoint_c)
            self.stability.observe(self.forehearth_temp.setpoint_c)
            self._wait(self.config.forehearth.stable_hold_s)
            self.stability.observe(self.forehearth_temp.setpoint_c)
            self._note(steps, "stability_warm", epoch=self.stability.epoch)

            gate = self.gate.warm_up(self.stability)
            self._note(steps, "gate_open", opening_pct=round(gate.opening_pct, 2))

            gob = self.gob.form(self.config.feeder.gob_mass_target_g)
            self._note(steps, "gob_form", mass_g=round(gob.mass_g, 3))

            cut = self.shear.feed(gob)
            self._note(steps, "shear", gob_index=cut.gob_index)

            run = self.forming.run(gob, self.closer, self.blow, self.filler)
            self._note(steps, "mold_cycle", closure_epoch=run.closure_epoch)

            self.evaluate_anneal(self.config.temp.anneal_target_c)
            self._note(
                steps,
                "anneal_start",
                belt_speed=round(self.belt.speed_m_per_min, 3),
                belt_normal=self.belt.is_normal(),
            )

            self._started = True
            return self.startup_report()

    def stop(self) -> dict:
        """执行停炉顺序：关闸板 → 关助燃风 → 停排烟风机。"""

        with self._lock:
            self._shutdown_steps = []
            steps = self._shutdown_steps
            self.gate.close("停炉")
            self._note(steps, "gate_close")
            shutdown = close_combustion_air(self.air, self.draft, self.bus)
            for step in shutdown["steps"]:
                steps.append("air_close" if step == "combustion_air_close" else "draft_stop")
            self.charger.interrupt("停炉")
            self.level.clear()
            self._started = False
            return self.shutdown_report()

    def cycle(self, inputs: CycleInputs) -> dict:
        """推进一个控制周期，返回本周期摘要。"""

        state = inputs.clamped()
        with self._lock:
            if not self._started:
                raise SequenceError("平台尚未启动，禁止推进控制周期", component="console")
            self._cycles += 1
            self._elapsed_s += state.dt
            moment = self.clock.now()

            load_c = state.load * 12.0
            melt_c = self.furnace_temp.target_c + load_c - 4.0
            if not self.igniter.is_burning() and not self.igniter.is_latched():
                self.igniter.reignite(melt_c + 30.0, "窑温回落")
            self.furnace_temp.observe(melt_c)

            self.level.drift(-7.0 * state.pull_rate - 1.0 * state.dt)
            charge_kg = 0.0
            if self.level.snapshot()["level_ok"]:
                charge = self.charger.start(self.level)
                charge_kg = charge.batch_kg
                self.level.drift(9.0 * state.dt)

            plan = self.mix.plan(self.scale.baseline())
            forehearth_c = self.forehearth_temp.setpoint_c - 3.0 * (1.0 - state.load)
            self.forehearth_temp.observe(forehearth_c)
            stability = self.stability.observe(forehearth_c)
            if stability.stable and not self.gate.is_open():
                self.gate.warm_up(self.stability)
            elif not stability.stable and self.gate.is_open():
                self.gate.close("料道温度漂移")

            gob = self.gob.form(state.gob_mass_g)
            cut = self.shear.feed(gob)
            run = self.forming.run(gob, self.closer, self.blow, self.filler)

            anneal = self.evaluate_anneal(self._anneal_temperature(state.load))

            self.trend.append("melt_temp", moment, self.furnace_temp.temperature_c)
            self.trend.append("level_mm", moment, self.level.level_mm)
            self.trend.append("forehearth_temp", moment, self.forehearth_temp.temperature_c)
            self.trend.append("gob_mass_g", moment, gob.mass_g)
            self.trend.append("belt_speed", moment, anneal["belt"]["speed_m_per_min"])
            self.trend.append("heater_power_pct", moment, anneal["heater"]["power_pct"])

            return {
                "cycle": self._cycles,
                "dt": state.dt,
                "load": round(state.load, 4),
                "melt_temp_c": round(self.furnace_temp.temperature_c, 3),
                "level": self.level.snapshot(),
                "charge": {
                    "charged_kg": round(charge_kg, 3),
                    "total_kg": round(self.charger.charged_kg, 3),
                },
                "mix": plan.as_dict(),
                "stability": stability.as_dict(),
                "gate": self.gate.snapshot(),
                "gob": gob.as_dict(),
                "cut": cut.as_dict(),
                "forming": run.as_dict(),
                "anneal": anneal,
                "zones": self.zones.snapshot(),
            }

    def evaluate_anneal(self, temperature_c: float) -> dict:
        """按退火温度结算告警、闩锁、网带、加热器与段温。"""

        setpoint = self.curve.setpoint(self._elapsed_s).setpoint_c
        alarm_active = self.alarm.observe(temperature_c)
        self.latch.follow_alarm(self.alarm)
        belt = self.belt.step(self.latch)
        heater = self.heater.step(setpoint, self.anneal.mean_c(), self.latch)
        self.anneal.apply(self.curve.zone_setpoints(self._elapsed_s), self._elapsed_s)
        self.curve.report(self._elapsed_s)
        return {
            "setpoint_c": round(setpoint, 3),
            "temperature_c": round(float(temperature_c), 3),
            "alarm": self.alarm.snapshot(),
            "alarm_active": alarm_active,
            "latch": self.latch.snapshot(),
            "belt": belt.as_dict(),
            "heater": heater.as_dict(),
            "anneal_mean_c": round(self.anneal.mean_c(), 3),
        }

    def _anneal_temperature(self, load: float) -> float:
        """按曲线给定值与加热器上一拍的出力推算退火段实测温度。"""

        overshoot = (self.heater.power_pct - 50.0) * 0.5
        return self.anneal.mean_c() + overshoot + float(load) * 4.0

    def start_charging(self, path: str = "start") -> dict:
        """按给定路径投料：正常启动走 start，联锁恢复走 resume。"""

        with self._lock:
            if path == "resume":
                outcome = self.charger.resume(self.level)
            elif path == "start":
                outcome = self.charger.start(self.level)
            else:
                raise SequenceError(f"未知的投料路径: {path}", component="batch")
            self.level.drift(9.0)
            payload = outcome.as_dict()
            payload["weighed_kg"] = round(self.scale.weigh(outcome.batch_kg), 3)
            return payload

    def interrupt_charging(self, reason: str) -> dict:
        """投料联锁：置位待恢复标记，供恢复路径使用。"""

        self.charger.interrupt(reason)
        return {"interrupted": self.charger.interrupted, "reason": reason}

    def ignite_burner(self, flame_temperature_c: float) -> dict:
        with self._lock:
            return self.igniter.ignite(flame_temperature_c)

    def reignite_burner(self, flame_temperature_c: float, reason: str) -> dict:
        with self._lock:
            return self.igniter.reignite(flame_temperature_c, reason)

    def release_ignition_latch(self, reason: str = "控制台人工确认") -> dict:
        """人工确认解除点火闩锁：吹扫后回待机，两路连败清零。"""

        with self._lock:
            return self.igniter.release_latch(
                self.config.burner.air_min_flow_m3_h * 0.9, reason=reason
            )

    def open_forehearth_gate(self, path: str = "warm_up") -> dict:
        """按给定路径开闸板：初次升温走 warm_up，再升温走 reheat。"""

        with self._lock:
            if path == "reheat":
                outcome = self.gate.reheat(self.stability)
            elif path == "warm_up":
                outcome = self.gate.warm_up(self.stability)
            else:
                raise SequenceError(f"未知的开闸板路径: {path}", component="forehearth")
            return outcome.as_dict()

    def blow_mold(self, path: str = "cycle", reason: str = "控制台触发") -> dict:
        """按给定路径吹气：正常循环走 cycle，补料重试走 retry。"""

        with self._lock:
            if path == "retry":
                gob = self.gob.form(self.gob.recover_mass())
                run = self.forming.retry(gob, self.closer, self.blow, self.filler, reason)
                return {"retry": True, "gob": gob.as_dict(), "cycle": run.as_dict()}
            closure = self.closer.close()
            outcome = self.blow.normal(closure)
            return {"retry": False, "closure": closure.as_dict(), "blow": outcome.as_dict()}

    def reset_lehr(self, reason: str = "退火复位") -> dict:
        """退火告警复位并释放闩锁，随后让网带与加热器回到正常档。"""

        with self._lock:
            cleared = self.alarm.clear(reason)
            released = self.latch.release_after_clear(self.alarm)
            belt = self.belt.step(self.latch)
            for _ in range(8):
                if belt.normal:
                    break
                belt = self.belt.step(self.latch)
            setpoint = self.curve.setpoint(self._elapsed_s).setpoint_c
            heater = self.heater.step(setpoint, self.anneal.mean_c(), self.latch)
            return {
                "alarm_cleared": cleared,
                "latch_released": released,
                "latch": self.latch.snapshot(),
                "belt": belt.as_dict(),
                "heater": heater.as_dict(),
            }

    def recalibrate_scale(self, offset_kg: float) -> dict:
        """重新标定投料秤并让下一批配比走新基线。"""

        with self._lock:
            baseline = self.scale.recalibrate(offset_kg, self.clock.now())
            plan = self.mix.plan(self.scale.baseline())
            return {
                "baseline": baseline.as_dict(),
                "plan": plan.as_dict(),
                "fresh": self.mix.matches(plan, self.scale.baseline()),
            }

    def rebuild_zone_map(self, assignments: dict, reason: str = "窑体大修") -> dict:
        """窑体大修后重建分区映射。"""

        with self._lock:
            revision = self.zones.rebuild(assignments, reason)
            return {"revision": revision, "zones": self.zones.describe()}

    def trip_burner(self, zone: str, reason: str = "窑温越限") -> dict:
        """跳闸联动：按当前分区映射关断对应燃烧器。"""

        with self._lock:
            outcome = self.trip.trip(zone, reason)
            return outcome.as_dict()

    def reset_trip(self, reason: str = "跳闸复位") -> dict:
        with self._lock:
            was = self.trip.reset(reason)
            return {"was_tripped": was, "trip": self.trip.snapshot()}

    def clear_furnace(self, reason: str = "出料清炉") -> dict:
        """出料或清炉：作废液面确认凭据并置位投料待恢复标记。"""

        with self._lock:
            epoch = self.level.clear()
            self.charger.interrupt(reason)
            return {"level_epoch": epoch, "charger": self.charger.snapshot()}

    def startup_report(self) -> dict:
        """启动执行报告：步骤、顺序自查与关键量。"""

        steps = list(self._startup_steps)
        return {
            "steps": steps,
            "violations": self.verify_order(steps, START_ORDER_RULES),
            "topology_missing": self.verify_topology(),
            "level_mm": round(self.level.level_mm, 3),
            "melt_temp_c": round(self.furnace_temp.temperature_c, 3),
            "gate_opening_pct": round(self.gate.opening_pct, 2),
            "belt_speed_m_per_min": round(self.belt.speed_m_per_min, 3),
            "zone_revision": self.zones.revision,
        }

    def shutdown_report(self) -> dict:
        """停炉执行报告：步骤与停机顺序自查。"""

        steps = list(self._shutdown_steps)
        violations = self.verify_order(steps, STOP_ORDER_RULES)
        for item in air_closed_before_draft_stop(self.bus):
            if item not in violations:
                violations.append(item)
        return {
            "steps": steps,
            "violations": violations,
            "draft_running": self.draft.is_running,
            "air_established": self.air.is_established(),
        }

    def verify_order(
        self, steps: list[str], rules: tuple[tuple[str, str, str], ...]
    ) -> list[str]:
        """按执行记录自查顺序约束，返回违规说明列表（空表示合规）。"""

        violations: list[str] = []
        for earlier, later, description in rules:
            if earlier not in steps or later not in steps:
                violations.append(f"{description}：步骤 {earlier} 或 {later} 未执行")
                continue
            if steps.index(earlier) > steps.index(later):
                violations.append(f"{description}：实际先执行了 {later}")
        return violations

    def verify_topology(self) -> list[str]:
        """确认链路拓扑里确实声明了这些上下游关系。"""

        missing: list[str] = []
        for upstream, downstream in TOPOLOGY_RULES:
            if not self.topology.requires_before(upstream, downstream):
                missing.append(f"拓扑缺少 {upstream} -> {downstream}")
        return missing

    def readiness_report(self) -> dict:
        """投料、点火、开闸板、吹气与退火复位的前置条件。"""

        stability = self.stability.last_state()
        return {
            "started": self._started,
            "level": self.level.snapshot(),
            "scale_durable": self.scale.is_durable(),
            "draft_negative": self.draft.is_negative(),
            "air_established": self.air.is_established(),
            "melt_temp_ok": self.furnace_temp.is_ok(),
            "melt_temp_low": self.furnace_temp.is_low(),
            "ignition": self.igniter.snapshot(),
            "stability": stability.as_dict() if stability else None,
            "gate": self.gate.snapshot(),
            "forehearth_above_gate_min": self.forehearth_temp.is_above_gate_min(),
            "closure": self.closer.snapshot(),
            "mold_closed": self.closer.is_closed(),
            "anneal_alarm": self.alarm.snapshot(),
            "anneal_latch": self.latch.snapshot(),
            "zone_revision": self.zones.revision,
        }

    def note_error(self, error: GlassMeltError) -> dict:
        """把平台异常写入审计，返回落盘记录。"""

        payload = error.as_record()
        record = self.audit.record(
            AuditRecord.create(
                self.clock.now(),
                payload["component"],
                "error",
                payload["type"],
                severity="warning",
                message=payload["message"],
            )
        )
        return record.as_dict()

    def namespaces_by_area(self) -> dict:
        """按区域分组返回命名空间，控制台据此渲染分区清单。"""

        areas = sorted({entry["area"] for entry in self.namespaces.describe()})
        return {
            area: [entry.as_dict() for entry in self.namespaces.in_area(area)]
            for area in areas
        }

    def endpoints(self) -> dict:
        """返回本平台对外暴露的页面与接口清单。"""

        return {
            "pages": list(self.config.pages),
            "api": [
                "/api/state",
                "/api/trend",
                "/api/alarms",
                "/api/audit",
                "/api/journal",
                "/api/readiness",
                "/api/batch/start",
                "/api/burner/ignite",
                "/api/burner/release",
                "/api/burner/trip",
                "/api/forehearth/gate",
                "/api/mold/blow",
                "/api/lehr/reset",
                "/api/scale/calibrate",
                "/api/furnace/clear",
                "/api/furnace/rebuild",
                "/api/cycle",
                "/api/start",
                "/api/stop",
            ],
        }

    def topology_summary(self) -> dict:
        """链路拓扑加上每个节点的上下游关系。"""

        summary = self.topology.as_dict()
        summary["upstream"] = {
            name: self.topology.upstream_of(name) for name in self.topology.nodes
        }
        summary["downstream"] = {
            name: self.topology.downstream_of(name) for name in self.topology.nodes
        }
        return summary

    def status(self) -> dict:
        """汇总状态快照，供控制台与命令行读取。"""

        return {
            "platform": self.config.name,
            "project_id": self.config.project_id,
            "started": self._started,
            "cycles": self._cycles,
            "elapsed_s": round(self._elapsed_s, 3),
            "now": round(self.clock.now(), 3),
            "utc": self.clock.utc_iso(),
            "level": self.level.snapshot(),
            "melt_temp": self.furnace_temp.snapshot(),
            "mix": self.mix.snapshot(),
            "draft": self.draft.snapshot(),
            "gas": self.gas.snapshot(),
            "air": self.air.snapshot(),
            "ignition": self.igniter.snapshot(),
            "zones": self.zones.snapshot(),
            "trip": self.trip.snapshot(),
            "scale": self.scale.snapshot(),
            "charger": self.charger.snapshot(),
            "stability": self.stability.snapshot(),
            "forehearth": self.forehearth_temp.snapshot(),
            "gate": self.gate.snapshot(),
            "gob": self.gob.snapshot(),
            "shear": self.shear.snapshot(),
            "forming": self.forming.snapshot(),
            "mold": {
                "closer": self.closer.snapshot(),
                "blow": self.blow.snapshot(),
                "fill": self.filler.snapshot(),
            },
            "anneal": {
                "alarm": self.alarm.snapshot(),
                "latch": self.latch.snapshot(),
                "curve": self.curve.snapshot(self._elapsed_s),
                "belt": self.belt.snapshot(),
                "heater": self.heater.snapshot(),
                "zones": self.anneal.snapshot(),
            },
            "namespace_areas": sorted(
                {entry["area"] for entry in self.namespaces.describe()}
            ),
            "namespaces_by_area": self.namespaces_by_area(),
            "audit": self.audit.summary(),
            "endpoints": self.endpoints(),
        }


def build_platform(
    config: PlatformConfig | None = None,
    clock: Clock | None = None,
    storage_root: str | None = None,
    unit: str = DEFAULT_UNIT,
) -> Platform:
    """构造平台；``storage_root`` 给出时覆盖配置里的存储目录。"""

    effective = config or default_config()
    if storage_root:
        effective = PlatformConfig(
            name=effective.name,
            project_id=effective.project_id,
            storage_root=os.path.abspath(storage_root),
            furnace=effective.furnace,
            burner=effective.burner,
            forehearth=effective.forehearth,
            temp=effective.temp,
            feeder=effective.feeder,
            mold=effective.mold,
            lehr=effective.lehr,
            batch=effective.batch,
            pages=effective.pages,
        )
    return Platform(config=effective, clock=clock, unit=unit)
