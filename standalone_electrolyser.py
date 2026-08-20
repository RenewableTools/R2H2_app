#!/usr/bin/env python3
"""Standalone isothermal PEM electrolyser runner.

Edit the user configuration section below, then run:

    python standalone_electrolyser.py

The model is intentionally self-contained. It does not import from the rest of
the project, does not use the battery model, and keeps the electrolyser at a
fixed user-defined temperature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np


# ---------------------------------------------------------------------------
# User configuration
# ---------------------------------------------------------------------------

ISOTHERMAL_TEMPERATURE_C = 55.0
TIME_STEP_S = 1.0

# Replace this with any 1-D power-flow array in watts.
POWER_FLOW_W = np.concatenate(
    [
        np.zeros(300),
        np.full(900, 8.0e6),
        np.full(1200, 1.6e7),
        np.full(1200, 2.1e7),
    ]
)


# ---------------------------------------------------------------------------
# Model defaults
# ---------------------------------------------------------------------------

V_TN_CELL = 1.48
H2_LHV_J_PER_G = 119_988.0


@dataclass
class ElectroCellPEM:
    rR: float = 8.314
    rF: float = 96485.0
    iNumCurrent: int = 1000
    rA_cell: float = 1000.0
    rI_rated: float = 3.0
    rT_0: float = 20.0
    rT: float = ISOTHERMAL_TEMPERATURE_C
    rE_min0: float = 1.55
    rR_0: float = 0.345
    rD_rt: float = -0.0045
    rV_cellNom: float = 2.1
    rV_bank: float = 633.5
    rI_bank: float = 3000.0
    rF1: float = 0.25
    rF2: float = 0.996
    arCurrentDensity: Optional[np.ndarray] = None
    arE_min: Optional[np.ndarray] = None
    arR_cell: Optional[np.ndarray] = None
    arV_cell: Optional[np.ndarray] = None

    def build_curves(self) -> "ElectroCellPEM":
        current_density = np.linspace(0.0, self.rI_rated, self.iNumCurrent)
        e_min = self.rE_min0 + (self.rR * (273.15 + self.rT)) / (2.0 * self.rF)
        r_cell = (self.rT - self.rT_0) * self.rD_rt + self.rR_0
        v_cell = e_min - e_min * np.exp(-250.0 * current_density) + r_cell * current_density

        self.arCurrentDensity = current_density
        self.arE_min = np.full_like(current_density, e_min)
        self.arR_cell = np.full_like(current_density, r_cell)
        self.arV_cell = v_cell
        return self

    def faraday_efficiency(self, current_density: np.ndarray) -> np.ndarray:
        return (current_density**2 / (self.rF1 + current_density**2)) * self.rF2


@dataclass
class ElectrolyserUnit:
    iN_stacks: int = 4
    iN_banks: int = 2
    iNumElectro: int = 5
    iN_cell: int = 100
    iControlLevel: int = 2
    rTimeConst: float = 0.0
    rDegradation: float = 1e-30
    rTurnDownRatio: float = 0.125
    r_s: float = 1.42e-10
    r_f: float = 3.33e-7
    r_o: float = 1.47e-4
    rAncillaryPowerFrac: float = 0.0
    rDeadBandRatio: float = 2.0
    rRampUp_W_s: Optional[float] = 2.0e5
    rRampDown_W_s: Optional[float] = 5.0e5
    iNumUnits: int = 0
    rTotalTurnOns: int = 0
    rSummedDegradation: float = 1e-30
    rDegradationOnOffTotal: float = 0.0
    rDegradationSteadyTotal: float = 0.0
    rDegradationFatigueTotal: float = 0.0
    rRatedPower_s: float = 0.0
    rMinPower_s: float = 0.0
    rAncillaryPower_s: float = 0.0
    arV_s: Optional[np.ndarray] = None
    arV_sd: Optional[np.ndarray] = None
    arI_s: Optional[np.ndarray] = None
    arH2Dot_s: Optional[np.ndarray] = None
    arP_Total_s: Optional[np.ndarray] = None
    arEfficiency_s: Optional[np.ndarray] = None


@dataclass
class UnitCurves:
    power_W: np.ndarray
    current_A: np.ndarray
    voltage_V: np.ndarray
    useful_voltage_V: np.ndarray
    hydrogen_gps: np.ndarray


@dataclass
class ElectrolyserRunResult:
    time_s: np.ndarray
    power_flow_W: np.ndarray
    available_power_W: np.ndarray
    total_electrolyser_demand_W: np.ndarray
    unit_is_on: np.ndarray
    unit_power_W: np.ndarray
    unit_current_A: np.ndarray
    unit_voltage_V: np.ndarray
    unit_h2_gps: np.ndarray
    total_h2_gps: np.ndarray
    cumulative_h2_g: np.ndarray
    stack_efficiency: np.ndarray
    system_efficiency: np.ndarray
    degradation_V: np.ndarray
    turn_ons: np.ndarray
    unit_temperature_C: np.ndarray
    summary: Dict[str, float] = field(default_factory=dict)


def rainflow(series: np.ndarray) -> np.ndarray:
    values = np.asarray(series, dtype=float).ravel()
    if values.size < 3:
        return np.zeros((0, 3))

    nz = np.where(np.diff(values) != 0)[0] + 1
    if nz.size:
        values = np.concatenate(([values[0]], values[nz]))

    signs = np.sign(np.diff(values))
    changes = np.where(np.diff(signs) != 0)[0] + 1
    turning_points = values[np.unique(np.r_[0, changes, len(values) - 1])]
    if turning_points.size < 3:
        return np.zeros((0, 3))

    stack: List[float] = [float(turning_points[0])]
    counts: List[float] = []
    amplitudes: List[float] = []
    means: List[float] = []

    for value in turning_points[1:]:
        stack.append(float(value))
        while len(stack) >= 3:
            a0 = stack[-3]
            a1 = stack[-2]
            a2 = stack[-1]
            r01 = abs(a1 - a0)
            r12 = abs(a2 - a1)
            if r12 >= r01:
                counts.append(0.5)
                amplitudes.append(r01 * 0.5)
                means.append((a0 + a1) * 0.5)
                stack[-3] = a2
                del stack[-1]
                del stack[-1]
            else:
                break

    for idx in range(len(stack) - 1):
        a0 = stack[idx]
        a1 = stack[idx + 1]
        counts.append(0.5)
        amplitudes.append(abs(a1 - a0) * 0.5)
        means.append((a0 + a1) * 0.5)

    if not counts:
        return np.zeros((0, 3))
    return np.column_stack((counts, amplitudes, means))


def _rainflow_fatigue_sum(series: np.ndarray) -> float:
    cycles = rainflow(series)
    if cycles.size == 0:
        return 0.0
    return float(np.sum((2.0 * cycles[:, 1]) * cycles[:, 0]))


def _bounded_allocate(total_power: float, weights: np.ndarray, lower: np.ndarray, upper: np.ndarray) -> np.ndarray:
    count = len(weights)
    if count == 0:
        return np.zeros(0, dtype=float)

    total_power = float(total_power)
    weights = np.asarray(weights, dtype=float)
    weights = np.clip(weights, 0.0, np.inf)
    lower = np.asarray(lower, dtype=float)
    upper = np.asarray(upper, dtype=float)

    if np.sum(weights) <= 0.0:
        weights = np.ones(count, dtype=float)

    allocation = total_power * (weights / np.sum(weights))
    allocation = np.clip(allocation, lower, upper)

    for _ in range(30):
        remainder = total_power - float(np.sum(allocation))
        if abs(remainder) <= 1e-9:
            break

        if remainder > 0.0:
            free = allocation < (upper - 1e-12)
            if not np.any(free):
                break
            free_weights = weights[free]
            if np.sum(free_weights) <= 0.0:
                free_weights = np.ones(np.sum(free), dtype=float)
            increment = remainder * (free_weights / np.sum(free_weights))
            allocation[free] = np.minimum(upper[free], allocation[free] + increment)
        else:
            free = allocation > (lower + 1e-12)
            if not np.any(free):
                break
            free_weights = weights[free]
            if np.sum(free_weights) <= 0.0:
                free_weights = np.ones(np.sum(free), dtype=float)
            decrement = (-remainder) * (free_weights / np.sum(free_weights))
            allocation[free] = np.maximum(lower[free], allocation[free] - decrement)

    return allocation


class IsothermalElectrolyserModel:
    def __init__(self, cell: Optional[ElectroCellPEM] = None, unit_template: Optional[ElectrolyserUnit] = None):
        self.cell = cell or ElectroCellPEM(rT=ISOTHERMAL_TEMPERATURE_C)
        self.unit_template = unit_template or ElectrolyserUnit()
        self.cell.rT = ISOTHERMAL_TEMPERATURE_C
        self.cell.build_curves()
        self.units = self._build_units()
        self._populate_unit_curves()

    def _build_units(self) -> List[ElectrolyserUnit]:
        template = self.unit_template
        if template.iControlLevel == 1:
            template.iNumUnits = template.iNumElectro
        elif template.iControlLevel == 2:
            template.iNumUnits = template.iNumElectro * template.iN_banks
        else:
            template.iNumUnits = template.iNumElectro * template.iN_banks * template.iN_stacks

        units: List[ElectrolyserUnit] = []
        for _ in range(template.iNumUnits):
            unit = ElectrolyserUnit(**template.__dict__)
            unit.rSummedDegradation = float(template.rDegradation)
            unit.rTotalTurnOns = 0
            units.append(unit)
        return units

    def _populate_unit_curves(self) -> None:
        current_density = self.cell.arCurrentDensity
        faraday_eff = self.cell.faraday_efficiency(current_density)
        voltage_cell = self.cell.arV_cell
        area = self.cell.rA_cell
        r_mu = 2.01588
        r_f_const = 9.6485e4
        r_n_const = 2.0
        r_loss_dry = 0.03
        factor = r_mu / r_f_const / r_n_const * (1.0 - r_loss_dry)

        for unit in self.units:
            if unit.iControlLevel == 1:
                multiplier = unit.iN_cell * unit.iN_stacks * unit.iN_banks
            elif unit.iControlLevel == 2:
                multiplier = unit.iN_cell * unit.iN_stacks
            else:
                multiplier = unit.iN_cell

            voltage_useful = voltage_cell * multiplier
            voltage_degraded = (voltage_cell + unit.rSummedDegradation) * multiplier
            current = current_density * area
            hydrogen = faraday_eff * factor * current * multiplier
            power = current * voltage_degraded

            with np.errstate(divide="ignore", invalid="ignore"):
                efficiency = np.divide(
                    H2_LHV_J_PER_G * hydrogen,
                    power,
                    out=np.zeros_like(power),
                    where=power > 0.0,
                )

            unit.arV_s = voltage_useful
            unit.arV_sd = voltage_degraded
            unit.arI_s = current
            unit.arH2Dot_s = hydrogen
            unit.arP_Total_s = power
            unit.arEfficiency_s = efficiency
            unit.rRatedPower_s = float(power[-1])
            unit.rMinPower_s = unit.rRatedPower_s * unit.rTurnDownRatio
            unit.rAncillaryPower_s = unit.rAncillaryPowerFrac * unit.rRatedPower_s

    def _unit_curves(self, unit: ElectrolyserUnit) -> UnitCurves:
        return UnitCurves(
            power_W=np.asarray(unit.arP_Total_s, dtype=float),
            current_A=np.asarray(unit.arI_s, dtype=float),
            voltage_V=np.asarray(unit.arV_sd, dtype=float),
            useful_voltage_V=np.asarray(unit.arV_s, dtype=float),
            hydrogen_gps=np.asarray(unit.arH2Dot_s, dtype=float),
        )

    def run(self, power_flow_W: np.ndarray, dt_s: float = TIME_STEP_S) -> ElectrolyserRunResult:
        power_flow_W = np.asarray(power_flow_W, dtype=float).ravel()
        if power_flow_W.size == 0:
            raise ValueError("power_flow_W must contain at least one time step")
        if dt_s <= 0.0:
            raise ValueError("dt_s must be positive")

        num_units = len(self.units)
        num_steps = power_flow_W.size
        time_s = np.arange(num_steps, dtype=float) * float(dt_s)

        available_power_W = np.maximum(
            power_flow_W - sum(unit.rAncillaryPower_s for unit in self.units),
            0.0,
        )
        total_demand_W = np.zeros(num_steps, dtype=float)
        unit_is_on = np.zeros((num_units, num_steps), dtype=int)
        unit_power_W = np.zeros((num_units, num_steps), dtype=float)
        unit_current_A = np.zeros((num_units, num_steps), dtype=float)
        unit_voltage_V = np.zeros((num_units, num_steps), dtype=float)
        unit_h2_gps = np.zeros((num_units, num_steps), dtype=float)
        total_h2_gps = np.zeros(num_steps, dtype=float)
        stack_efficiency = np.zeros(num_steps, dtype=float)
        system_efficiency = np.zeros(num_steps, dtype=float)
        turn_ons = np.zeros(num_units, dtype=int)

        rated_power = float(self.units[0].rRatedPower_s)
        min_power = float(self.units[0].rMinPower_s)
        prev_is_on = np.zeros(num_units, dtype=int)

        for step in range(num_steps):
            total_available = float(available_power_W[step])
            current_is_on = prev_is_on.copy()
            total_on_prev = int(np.sum(prev_is_on))

            if total_available <= 0.0:
                prev_is_on = current_is_on
                continue

            min_units_required = int(np.ceil(total_available / rated_power)) if rated_power > 0.0 else 0
            max_units_supported = int(np.floor(total_available / min_power)) if min_power > 0.0 else num_units
            max_units_supported = max(0, min(max_units_supported, num_units))
            target_on = min(max(min_units_required, total_on_prev), max_units_supported)

            degradation = np.array([unit.rSummedDegradation for unit in self.units], dtype=float)
            rank_low_deg = np.argsort(degradation)
            rank_high_deg = rank_low_deg[::-1]

            current_on = int(np.sum(current_is_on))
            if target_on > current_on:
                need = target_on - current_on
                for idx in rank_low_deg:
                    if need <= 0:
                        break
                    if current_is_on[idx] == 0:
                        current_is_on[idx] = 1
                        turn_ons[idx] += 1
                        self.units[idx].rTotalTurnOns += 1
                        need -= 1
            elif target_on < current_on:
                need = current_on - target_on
                for idx in rank_high_deg:
                    if need <= 0:
                        break
                    if current_is_on[idx] == 1:
                        current_is_on[idx] = 0
                        need -= 1

            on_idx = np.flatnonzero(current_is_on > 0)
            if on_idx.size == 0:
                prev_is_on = current_is_on
                continue

            preferred = np.ones(on_idx.size, dtype=float)
            lower = np.full(on_idx.size, min_power, dtype=float)
            upper = np.full(on_idx.size, rated_power, dtype=float)
            feasible_total = min(total_available, float(np.sum(upper)))
            if feasible_total < float(np.sum(lower)):
                keep = int(np.floor(feasible_total / min_power)) if min_power > 0.0 else on_idx.size
                keep = max(0, min(keep, on_idx.size))
                current_is_on[:] = 0
                if keep > 0:
                    kept = on_idx[:keep]
                    current_is_on[kept] = 1
                    on_idx = kept
                    lower = np.full(on_idx.size, min_power, dtype=float)
                    upper = np.full(on_idx.size, rated_power, dtype=float)
                    preferred = np.ones(on_idx.size, dtype=float)
                    feasible_total = min(total_available, float(np.sum(upper)))
                else:
                    prev_is_on = current_is_on
                    continue

            allocation = _bounded_allocate(feasible_total, preferred, lower, upper)
            for local_idx, unit_idx in enumerate(on_idx):
                unit = self.units[unit_idx]
                prev_power = float(unit_power_W[unit_idx, step - 1]) if step > 0 else 0.0
                ramp_up = unit.rRampUp_W_s if unit.rRampUp_W_s is not None else np.inf
                ramp_down = unit.rRampDown_W_s if unit.rRampDown_W_s is not None else np.inf
                demand = float(np.clip(
                    allocation[local_idx],
                    prev_power - ramp_down * dt_s,
                    prev_power + ramp_up * dt_s,
                ))
                demand = float(np.clip(demand, min_power, rated_power))
                unit_power_W[unit_idx, step] = demand

                curves = self._unit_curves(unit)
                current = float(np.interp(demand, curves.power_W, curves.current_A))
                voltage = float(np.interp(current, curves.current_A, curves.voltage_V))
                useful_voltage = float(np.interp(current, curves.current_A, curves.useful_voltage_V))
                hydrogen = float(np.interp(current, curves.current_A, curves.hydrogen_gps))

                unit_current_A[unit_idx, step] = current
                unit_voltage_V[unit_idx, step] = voltage
                unit_h2_gps[unit_idx, step] = hydrogen

                total_demand_W[step] += demand
                total_h2_gps[step] += hydrogen

                if total_demand_W[step] > 0.0:
                    stack_efficiency[step] = (H2_LHV_J_PER_G * total_h2_gps[step]) / total_demand_W[step]
                if power_flow_W[step] > 0.0:
                    system_efficiency[step] = (H2_LHV_J_PER_G * total_h2_gps[step]) / power_flow_W[step]

                if unit.iControlLevel == 3:
                    vtn = V_TN_CELL * unit.iN_cell
                elif unit.iControlLevel == 2:
                    vtn = V_TN_CELL * unit.iN_cell * unit.iN_stacks
                else:
                    vtn = V_TN_CELL * unit.iN_cell * unit.iN_stacks * unit.iN_banks
                _ = current * max(voltage - vtn, 0.0)
                _ = useful_voltage

            unit_is_on[:, step] = current_is_on
            prev_is_on = current_is_on

        cumulative_h2_g = np.cumsum(total_h2_gps * dt_s)
        degradation_V = np.zeros(num_units, dtype=float)
        for idx, unit in enumerate(self.units):
            seg_v = np.where(unit_is_on[idx] > 0, unit_voltage_V[idx], 0.0)
            steady = unit.r_s * float(np.sum(seg_v) * dt_s)
            fatigue = unit.r_f * _rainflow_fatigue_sum(seg_v) if np.any(seg_v != 0.0) else 0.0
            on_off = unit.r_o * float(turn_ons[idx])
            total_deg = steady + fatigue + on_off
            unit.rDegradationSteadyTotal += steady
            unit.rDegradationFatigueTotal += fatigue
            unit.rDegradationOnOffTotal += on_off
            unit.rSummedDegradation += total_deg
            degradation_V[idx] = unit.rSummedDegradation

        summary = {
            "total_h2_kg": float(cumulative_h2_g[-1] / 1000.0),
            "peak_power_MW": float(np.max(total_demand_W) / 1e6),
            "mean_stack_efficiency": float(np.mean(stack_efficiency[total_demand_W > 0.0])) if np.any(total_demand_W > 0.0) else 0.0,
            "mean_system_efficiency": float(np.mean(system_efficiency[power_flow_W > 0.0])) if np.any(power_flow_W > 0.0) else 0.0,
            "temperature_C": float(ISOTHERMAL_TEMPERATURE_C),
        }

        return ElectrolyserRunResult(
            time_s=time_s,
            power_flow_W=power_flow_W,
            available_power_W=available_power_W,
            total_electrolyser_demand_W=total_demand_W,
            unit_is_on=unit_is_on,
            unit_power_W=unit_power_W,
            unit_current_A=unit_current_A,
            unit_voltage_V=unit_voltage_V,
            unit_h2_gps=unit_h2_gps,
            total_h2_gps=total_h2_gps,
            cumulative_h2_g=cumulative_h2_g,
            stack_efficiency=stack_efficiency,
            system_efficiency=system_efficiency,
            degradation_V=degradation_V,
            turn_ons=turn_ons,
            unit_temperature_C=np.full(num_units, ISOTHERMAL_TEMPERATURE_C, dtype=float),
            summary=summary,
        )


def main() -> None:
    model = IsothermalElectrolyserModel()
    result = model.run(POWER_FLOW_W, dt_s=TIME_STEP_S)

    print("Standalone isothermal electrolyser run")
    print(f"Temperature        : {ISOTHERMAL_TEMPERATURE_C:.1f} C")
    print(f"Time step          : {TIME_STEP_S:.3f} s")
    print(f"Samples            : {len(result.time_s)}")
    print(f"Units              : {len(model.units)}")
    print(f"Peak demand        : {result.summary['peak_power_MW']:.3f} MW")
    print(f"Total H2 produced  : {result.summary['total_h2_kg']:.3f} kg")
    print(f"Mean stack eta     : {result.summary['mean_stack_efficiency']:.4f}")
    print(f"Mean system eta    : {result.summary['mean_system_efficiency']:.4f}")
    print(f"Turn-ons total     : {int(np.sum(result.turn_ons))}")


if __name__ == "__main__":
    main()