#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Test Comment "Hello World"
###############################################################################################################
# Standard Python Libraries
import os
import copy
import socket
import time
import datetime
import ast
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Literal, Optional, Tuple
import sys
import numpy as np
from copy import deepcopy
from scipy import signal


# Custom Libraries
from r2h2.config import Paths
from r2h2.components import *

###############################################################################################################

# ---------------------------------------------------------------------------
# Electrochemistry constant
# ---------------------------------------------------------------------------
V_TN_CELL = 1.48  # [V] thermoneutral voltage per cell

# ---------------------------------------------------------------------------
# Bank thermal models
# ---------------------------------------------------------------------------

@dataclass
class BaseBankThermal(ABC):
    n_stacks: int = 4

    s1: float = 0.004
    r3: float = 1.0
    T_nominal: float = 55.0

    h1: float = 0.0
    h2: float = 10.0
    h3: float = 20.0
    k1: float = 52.0

    k_gen: float = 1.0
    c_coolant: float = 4180.0

    insulated: bool = False

    _s2_unins: float = 1e-4
    _k2_unins: float = 52.0
    _s2_ins: float = 0.2
    _k2_ins: float = 0.05

    @property
    def s2(self) -> float:
        return self._s2_ins if self.insulated else self._s2_unins

    @property
    def k2(self) -> float:
        return self._k2_ins if self.insulated else self._k2_unins


@dataclass
class BankThermalPEM(BaseBankThermal):
    W_stack: float = 0.48
    H_stack: float = 0.71
    L_stack: float = 1.43

    rho_core: float = 2240.0
    c_core: float = 710.0
    mass_div: int = 5
    c_other: float = 900.0
    rho_other: float = 1000.0

    T_nominal: float = 55.0
    T_min: float = 10.0
    T_max: float = 55.0
    rT: float = 55.0


@dataclass
class CurtailmentState:
    """Persistent state for wind curtailment command enforcement.

    The command itself is a cap in MW provided by a custom controller hook.
    Core enforcement applies:
    - minimum hold duration between accepted command changes
    - cap ramp-rate limit in W/s based on number of electrolysers
    """

    hold_seconds: int = 1800
    requested_cap_w: Optional[float] = None
    ramped_cap_w: Optional[float] = None
    last_change_second: Optional[int] = None
    next_hour_command_mw: Any = None


# ---------------------------------------------------------------------------
# Technology presets  (PEM only for now; ALK requires external UNIFI package)
# ---------------------------------------------------------------------------

_TECH_PRESETS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "PEM": {
        "topology": {
            "iN_stacks": 4,
            "iN_banks": 2,
            "iNumElectro": 5,
            "iN_cell": 100,
        },
        "dynamics": {
            "rTimeConst": 0.0,
            "rDeadBandRatio": 2.0,
            "r_s": 1.42e-10,
            "r_f": 3.33e-7,
            "r_o": 1.47e-4,
            "rRampUp_W_s": 2.0e5,
            "rRampDown_W_s": 5.0e5,
        },
    },
}


def _preset_section(kind: str, section: str) -> Dict[str, Any]:
    key = kind.upper() if kind.upper() in _TECH_PRESETS else "PEM"
    return copy.deepcopy(_TECH_PRESETS[key].get(section, {}))


def apply_unit_topology(
    kind: str,
    el: "ElectrolyserUnit",
    overrides: Optional[Dict[str, int]] = None,
) -> "ElectrolyserUnit":
    params = _preset_section(kind, "topology")
    if overrides:
        params.update(overrides)
    for attr, value in params.items():
        setattr(el, attr, value)
    return el


def apply_unit_profile(
    kind: str,
    el: "ElectrolyserUnit",
    overrides: Optional[Dict[str, float]] = None,
) -> "ElectrolyserUnit":
    params = _preset_section(kind, "dynamics")
    if overrides:
        params.update(overrides)
    for attr, value in params.items():
        setattr(el, attr, value)
    return el


def bank_thermal_from_kind(
    kind: str,
    el: Optional["ElectrolyserUnit"] = None,
    *,
    insulated: bool = False,
) -> BaseBankThermal:
    template = _preset_section(kind, "topology")
    n_stacks_bank = el.iN_stacks if el else template.get("iN_stacks", 4)
    model = BankThermalPEM(n_stacks=n_stacks_bank)
    model.insulated = insulated
    return model


# ---------------------------------------------------------------------------
# Thermal step  (PEM implementation; mirrors UNIFI_alk_model_thermal API)
# ---------------------------------------------------------------------------

def thermal_step(
    bank: BaseBankThermal,
    *,
    Q_el: float,
    T_amb: float,
    dt: float,
    out: Optional[dict] = None,
) -> BaseBankThermal:
    """Advance *bank* temperature by *dt* seconds given electrical heat *Q_el* [W]."""
    if out is None:
        out = {}

    T = bank.rT

    # ── Thermal capacitance (J/K) ───────────────────────────────────────────
    if isinstance(bank, BankThermalPEM):
        V_stack = bank.W_stack * bank.H_stack * bank.L_stack
        m_core = bank.rho_core * V_stack * bank.n_stacks
        C_th = (m_core / bank.mass_div) * bank.c_core + \
               (m_core * (1.0 - 1.0 / bank.mass_div)) * bank.c_other
    else:
        C_th = max(1e6 * bank.n_stacks, 1.0)

    # ── Equivalent conductance (W/K) ────────────────────────────────────────
    if isinstance(bank, BankThermalPEM):
        A_wall = 2.0 * (
            bank.W_stack * bank.H_stack +
            bank.W_stack * bank.L_stack +
            bank.H_stack * bank.L_stack
        ) * bank.n_stacks
        R_wall = bank.s1 / max(bank.k1, 1e-12) + bank.s2 / max(bank.k2, 1e-12)
        G_eq = A_wall / max(R_wall + 1.0 / max(bank.h2, 1e-12), 1e-12)
    else:
        G_eq = 500.0 * bank.n_stacks

    # ── Losses and cooling ───────────────────────────────────────────────────
    Q_lost = G_eq * (T - T_amb)

    Q_cool = 0.0
    P_cool_elec = 0.0
    COP = 3.0
    T_nom = bank.T_nominal
    if T > T_nom:
        Q_cool = G_eq * (T - T_nom)
        P_cool_elec = Q_cool / COP

    # ── Update temperature ───────────────────────────────────────────────────
    dT = (Q_el - Q_lost - Q_cool) * dt / max(C_th, 1.0)
    T_max = getattr(bank, "T_max", 90.0)
    bank.rT = float(np.clip(T + dT, T_amb, T_max))

    out["q_gain"]      = float(Q_el)
    out["q_lost"]      = float(Q_lost)
    out["q_cool"]      = float(Q_cool)
    out["p_cool_elec"] = float(P_cool_elec)
    out["G_eq"]        = float(G_eq)
    out["C_th"]        = float(C_th)

    return bank


# ---------------------------------------------------------------------------
# Rainflow cycle counting
# ---------------------------------------------------------------------------

def rainflow(series: np.ndarray, dt: float = 1.0) -> np.ndarray:
    """Rainflow counter.  Returns array of shape (N, 3): [count, amp, mean].

    Optimised vs original: stack holds plain floats (no (value, index) tuples),
    and output is accumulated in three separate lists to avoid tuple allocation
    in the inner loop.  Result is identical; unused t_start column is gone.
    """
    x = np.asarray(series, dtype=float).ravel()
    nz = np.where(np.diff(x) != 0)[0] + 1
    if nz.size:
        x = np.concatenate(([x[0]], x[nz]))

    s   = np.sign(np.diff(x))
    chg = np.where(np.diff(s) != 0)[0] + 1
    y   = x[np.unique(np.r_[0, chg, len(x) - 1])]

    n = len(y)
    if n < 3:
        return np.zeros((0, 3))

    stk: list = list(y[:1])   # plain float stack
    oc: list = []; oa: list = []; om: list = []

    for k in range(1, n):
        stk.append(y[k])
        while len(stk) >= 3:
            a0 = stk[-3]; a1 = stk[-2]; a2 = stk[-1]
            R01 = abs(a1 - a0); R12 = abs(a2 - a1)
            if R12 >= R01:
                oc.append(0.5); oa.append(R01 * 0.5); om.append((a0 + a1) * 0.5)
                stk[-3] = a2; del stk[-1]; del stk[-1]
            else:
                break

    for i in range(len(stk) - 1):
        a0 = stk[i]; a1 = stk[i + 1]
        oc.append(0.5); oa.append(abs(a1 - a0) * 0.5); om.append((a0 + a1) * 0.5)

    if not oc:
        return np.zeros((0, 3))
    return np.column_stack((oc, oa, om))


def _rainflow_fatigue_sum(series: np.ndarray) -> float:
    """Return  sum(2 * amp * count)  without building the output array.

    Equivalent to ``float(np.sum((2 * rf[:, 1]) * rf[:, 0]))`` on the result
    of ``rainflow(series)``, but avoids all list/array allocation for the
    output cycles.  Used in the per-unit post-loop where only this scalar is
    needed.
    """
    x = np.asarray(series, dtype=float).ravel()
    nz = np.where(np.diff(x) != 0)[0] + 1
    if nz.size:
        x = np.concatenate(([x[0]], x[nz]))
    s   = np.sign(np.diff(x))
    chg = np.where(np.diff(s) != 0)[0] + 1
    y   = x[np.unique(np.r_[0, chg, len(x) - 1])]

    n = len(y)
    if n < 3:
        return 0.0

    stk: list = list(y[:1])
    total = 0.0

    for k in range(1, n):
        stk.append(y[k])
        while len(stk) >= 3:
            a0 = stk[-3]; a1 = stk[-2]; a2 = stk[-1]
            R01 = abs(a1 - a0); R12 = abs(a2 - a1)
            if R12 >= R01:
                total += R01 * 0.5   # == (2 * R01/2) * 0.5
                stk[-3] = a2; del stk[-1]; del stk[-1]
            else:
                break

    for i in range(len(stk) - 1):
        total += abs(stk[i + 1] - stk[i]) * 0.5

    return total


# ---------------------------------------------------------------------------
# Wind data loader
# ---------------------------------------------------------------------------

def load_wind_h5(path: str, turbine_rated_W: float = 5.447e6) -> "WindInputs":
    """Load a wind HDF5 file produced by MATLAB / the legacy pipeline.

    Expected datasets:
      ``/PowerInput``  – shape (hours, time_steps) or (time_steps, hours)
      ``/Time``        – 1-D time vector in seconds  [0 … 3699]
      ``/WindSpeed``   – 1-D hourly wind speed (m/s), one value per hour

    If ``PowerInput`` is entirely NaN (placeholder file), power is synthesised
    from ``WindSpeed`` using a simple cubic wind-speed-to-power model capped at
    ``turbine_rated_W``.

    Returns a :class:`WindInputs` instance with:
      ``arPowerInput`` – shape (time_steps, hours)   [W]
      ``arTime``       – 1-D array of seconds        [s]
    """
    try:
        import h5py
    except ImportError as exc:
        raise ImportError("h5py is required to load wind HDF5 files.  "
                          "Install it with:  pip install h5py") from exc

    with h5py.File(path, "r") as f:
        t  = f["/Time"][:].astype(np.float64).ravel()
        ws = f["/WindSpeed"][:].astype(np.float64).ravel()
        # Use float32 to halve wind-array memory (e.g. 780 MB → 390 MB for a 3-year run).
        # The simulation inner loop promotes to float64 on first arithmetic use.
        P  = f["/PowerInput"][:].astype(np.float32)

    # Detect orientation: rows → time-steps (len t), cols → hours (len ws)
    if P.shape[0] == ws.size and P.shape[1] == t.size:
        P = P.T  # (hours, time) → (time, hours)

    # If power data is all-NaN, synthesise from wind speed
    if np.all(np.isnan(P)):
        v_cut_in  = 3.0    # m/s
        v_rated   = 12.0   # m/s
        v_cut_out = 25.0   # m/s
        T_steps   = t.size
        n_hours   = ws.size
        P = np.zeros((T_steps, n_hours), dtype=np.float32)
        for h in range(n_hours):
            v = ws[h]
            if v < v_cut_in or v >= v_cut_out:
                p = 0.0
            elif v >= v_rated:
                p = turbine_rated_W
            else:
                p = turbine_rated_W * ((v - v_cut_in) / (v_rated - v_cut_in)) ** 3
            P[:, h] = p  # constant within the hour (uniform profile)

    wind = WindInputs()
    wind.arPowerInput = P
    wind.arTime = t
    # Keep the hourly wind-speed series so downstream logging/UI can use the
    # exact run input instead of re-reading mutable simulation links later.
    setattr(wind, 'arWindSpeed', ws)
    return wind


# ---------------------------------------------------------------------------
# Low-pass filter (first-order IIR, matches legacy lsim_first_order_lowpass)
# ---------------------------------------------------------------------------

def lsim_first_order_lowpass(u: np.ndarray, t: np.ndarray, tau: float) -> np.ndarray:
    num = [1.0]
    den = [tau, 1.0]
    system = signal.TransferFunction(num, den)
    dt = float(t[1] - t[0])
    discrete_system = system.to_discrete(dt)
    return signal.lfilter(discrete_system.num, discrete_system.den, u)


def _normalise_curtailment_cap_mw(value: Any) -> Optional[float]:
    """Normalise a user/controller cap value to a finite non-negative MW scalar."""
    if value is None:
        return None
    try:
        cap_mw = float(value)
    except Exception:
        return None
    if not np.isfinite(cap_mw):
        return None
    return max(cap_mw, 0.0)


def _resolve_curtailment_cap_mw(
    provider,
    *,
    sim_second: int,
    sim_hour: int,
    second_in_hour: int,
    total_steps: int,
    transient_steps: int,
    units,
    battery,
    settings,
    state: CurtailmentState,
) -> Optional[float]:
    """Fetch an optional curtailment cap command from a provider.

    Supported provider forms:
    - scalar (float-like): constant cap in MW
    - callable: preferred signature uses keyword args; fallback positional call
    """
    if provider is None:
        return None

    if not callable(provider):
        scalar = _normalise_curtailment_cap_mw(provider)
        if scalar is not None:
            return scalar

        try:
            arr = np.asarray(provider, dtype=float).ravel()
        except Exception:
            return None
        if arr.size == 0:
            return None
        if arr.size == 1:
            return _normalise_curtailment_cap_mw(arr[0])

        t_steps = max(int(total_steps), 1)
        step0 = max(int(transient_steps), 0)
        idx = int(second_in_hour)

        # If a trimmed T_ctrl vector is provided, map early transient seconds
        # to the first control sample and shift thereafter.
        if arr.size == max(t_steps - step0, 0) and arr.size > 0:
            idx = max(idx - step0, 0)

        idx = max(0, min(idx, int(arr.size) - 1))
        return _normalise_curtailment_cap_mw(arr[idx])

    try:
        cmd = provider(
            sim_second=sim_second,
            sim_hour=sim_hour,
            second_in_hour=second_in_hour,
            units=units,
            battery=battery,
            settings=settings,
            state=state,
        )
    except TypeError:
        # Backward-compatible fallback for a simple positional callable.
        try:
            cmd = provider(sim_second, sim_hour, second_in_hour)
        except Exception:
            return None
    except Exception:
        return None

    return _normalise_curtailment_cap_mw(cmd)


def _apply_curtailment_on_raw_wind(
    raw_power: np.ndarray,
    *,
    dt: float,
    hour_index_abs: int,
    units,
    battery,
    settings,
    state: Optional[CurtailmentState],
    cap_provider=None,
    transient_steps: int = 0,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Apply curtailment to raw wind power before any downstream calculations.

    The ramp limit applies to the cap trajectory only. Realized wind can drop
    faster than the negative cap-ramp limit when raw wind naturally drops below
    the current ramped cap.
    """
    raw = np.asarray(raw_power, dtype=float).ravel()
    effective = raw.copy()
    req_cap_w = np.full(raw.shape, np.nan, dtype=float)
    app_cap_w = np.full(raw.shape, np.nan, dtype=float)

    if state is None:
        return effective, req_cap_w, app_cap_w

    dt_s = max(float(dt), 0.0)
    n_electro = max(int(getattr(units[0], 'iNumElectro', 1)), 1)
    ramp_limit_w_s = 0.1 * 1e6 * n_electro
    step_limit_w = ramp_limit_w_s * dt_s
    hour_offset_s = int(round(hour_index_abs * raw.size * dt_s))

    for k in range(raw.size):
        abs_sec = hour_offset_s + int(round(k * dt_s))
        cmd_mw = _resolve_curtailment_cap_mw(
            cap_provider,
            sim_second=abs_sec,
            sim_hour=hour_index_abs,
            second_in_hour=k,
            total_steps=raw.size,
            transient_steps=transient_steps,
            units=units,
            battery=battery,
            settings=settings,
            state=state,
        )

        if cmd_mw is not None:
            cmd_w = cmd_mw * 1e6
            if state.requested_cap_w is None:
                state.requested_cap_w = cmd_w
                state.last_change_second = abs_sec
            elif abs(cmd_w - state.requested_cap_w) > 1e-9:
                can_change = (
                    state.last_change_second is None
                    or (abs_sec - state.last_change_second) >= int(state.hold_seconds)
                )
                if can_change:
                    state.requested_cap_w = cmd_w
                    state.last_change_second = abs_sec

        if state.requested_cap_w is None:
            effective[k] = raw[k]
            continue

        if state.ramped_cap_w is None or not np.isfinite(state.ramped_cap_w):
            state.ramped_cap_w = max(raw[k], 0.0)

        ramped_cap_w = float(state.ramped_cap_w)
        delta = state.requested_cap_w - ramped_cap_w
        if step_limit_w <= 0.0:
            ramped_cap_w = state.requested_cap_w
        elif delta > step_limit_w:
            ramped_cap_w += step_limit_w
        elif delta < -step_limit_w:
            ramped_cap_w -= step_limit_w
        else:
            ramped_cap_w = state.requested_cap_w

        state.ramped_cap_w = max(ramped_cap_w, 0.0)
        req_cap_w[k] = state.requested_cap_w
        app_cap_w[k] = state.ramped_cap_w
        effective[k] = min(raw[k], state.ramped_cap_w)

    return effective, req_cap_w, app_cap_w


# ---------------------------------------------------------------------------
# Bank-index mapping helper
# ---------------------------------------------------------------------------

def _make_bank_index_maps(units: list) -> Tuple[List[List[int]], np.ndarray]:
    u0 = units[0]
    banks_per_electro = u0.iN_banks
    stacks_per_bank = u0.iN_stacks if u0.iControlLevel == 3 else 1
    units_per_electro = banks_per_electro * stacks_per_bank
    num_banks_total = u0.iNumElectro * banks_per_electro
    num_units = len(units)

    bank_to_units: List[List[int]] = [[] for _ in range(num_banks_total)]
    unit_to_bank = np.zeros(num_units, dtype=int)

    for i in range(num_units):
        e_idx = i // units_per_electro
        within = i % units_per_electro
        b_local = within // stacks_per_bank
        b_global = e_idx * banks_per_electro + b_local
        unit_to_bank[i] = b_global
        bank_to_units[b_global].append(i)

    return bank_to_units, unit_to_bank


# ---------------------------------------------------------------------------
# Controller interface helpers
# ---------------------------------------------------------------------------

def _prefill_transient_slots(t_out, t_out_prev, step0: int) -> None:
    """Pre-fill the transient portion of controller-output arrays.

    Copies the last sample of each dispatch array from *t_out_prev* into the
    first *step0* timesteps of *t_out*, so the warm-up window shows a
    meaningful continuation of the previous hour rather than zeros.

    Also seeds all columns of ``aiIsOn`` (including ``step0:`` onwards) with
    the previous on/off state so both controllers start from a
    fully-populated baseline.
    """
    # 1D dispatch arrays
    for attr in ('arTotalElectroDemand', 'arTotalElectroOn',
                 'arElectroAvailablePowerA', 'arElectroAvailablePower'):
        src = getattr(t_out_prev, attr, None)
        dst = getattr(t_out, attr, None)
        if src is not None and dst is not None:
            try:
                dst[:step0] = float(np.asarray(src).ravel()[-1])
            except Exception:
                pass

    # 2D dispatch arrays (axis 1 = time)
    for attr in ('arProportionPower', 'arElectroDemand'):
        src = getattr(t_out_prev, attr, None)
        dst = getattr(t_out, attr, None)
        if src is not None and dst is not None:
            try:
                dst[:, :step0] = np.asarray(src)[:, -1:]
            except Exception:
                pass

    # Seed ALL columns of aiIsOn (transient + dispatch window) with prev state.
    src_on = getattr(t_out_prev, 'aiIsOn', None)
    dst_on = getattr(t_out, 'aiIsOn', None)
    if src_on is not None and dst_on is not None:
        try:
            prev_col = np.asarray(src_on)[:, -1:]
            dst_on[:, :] = prev_col
        except Exception:
            pass


def _make_controller_view(
    t_out, step0: int, T_ctrl: int, num_units: int
) -> SimpleNamespace:
    """Return a trimmed copy of *t_out* containing only the non-transient region.

    The returned :class:`~types.SimpleNamespace` holds independent copies (not
    numpy views) so it is safe to pass into a ``ThreadPoolExecutor``.
    ``arTime`` is re-zeroed so the controller sees the hour starting at t=0.
    ``arTotalElectroOn[-1]`` is pre-seeded with the previous on/off count so
    the dispatch loop's first iteration has a valid ``k-1`` reference.
    """
    ctrl = SimpleNamespace()

    # ── Read inputs ────────────────────────────────────────────────────────
    ctrl.arAvailablePower = t_out.arAvailablePower[step0:].copy()
    raw_time = getattr(t_out, 'arTime', None)
    if raw_time is not None and len(raw_time) > step0:
        ctrl.arTime = (np.asarray(raw_time)[step0:]
                       - np.asarray(raw_time)[step0]).copy()
    else:
        ctrl.arTime = np.arange(T_ctrl, dtype=float)

    # ── Dispatch outputs (aiIsOn pre-seeded by _prefill_transient_slots) ──
    ctrl.aiIsOn            = t_out.aiIsOn[:, step0:].copy()
    ctrl.arTotalElectroOn  = np.zeros(T_ctrl)
    ctrl.arProportionPower = np.zeros((num_units, T_ctrl))
    ctrl.arTotalElectroDemand     = np.zeros(T_ctrl)
    ctrl.arElectroAvailablePowerA = np.zeros(T_ctrl)
    ctrl.arElectroAvailablePower  = np.zeros(T_ctrl)

    # Seed the last slot of arTotalElectroOn so the dispatch loop's first
    # iteration (k=0, k-1=-1) has the correct previous on-count.
    ctrl.arTotalElectroOn[-1] = float(np.sum(ctrl.aiIsOn[:, -1]))

    # ── Non-time-indexed counters ──────────────────────────────────────────
    ctrl.aiWarmedUp     = np.ones((num_units, T_ctrl), dtype=int)
    ctrl.aiNumOn        = np.zeros(num_units, dtype=int)
    ctrl.rPreviousValue = 0.0
    return ctrl


def _merge_controller_outputs(t_out, t_ctrl, step0: int) -> None:
    """Write trimmed controller outputs back into the full *t_out* at offset *step0*."""
    # 1D time-indexed arrays
    for attr in ('arTotalElectroDemand', 'arTotalElectroOn',
                 'arElectroAvailablePowerA', 'arElectroAvailablePower'):
        src = getattr(t_ctrl, attr, None)
        dst = getattr(t_out, attr, None)
        if src is not None and dst is not None:
            try:
                dst[step0:] = src
            except Exception:
                pass

    # 2D time-indexed arrays (axis 1 = time)
    for attr in ('aiIsOn', 'arProportionPower'):
        src = getattr(t_ctrl, attr, None)
        dst = getattr(t_out, attr, None)
        if src is not None and dst is not None:
            try:
                dst[:, step0:] = src
            except Exception:
                pass

    # aiWarmedUp: transient slots keep np.ones (set at init); copy ctrl window.
    src_wu = getattr(t_ctrl, 'aiWarmedUp', None)
    dst_wu = getattr(t_out, 'aiWarmedUp', None)
    if src_wu is not None and dst_wu is not None:
        try:
            dst_wu[:, step0:] = src_wu
        except Exception:
            pass

    # Non-time-indexed state
    t_out.aiNumOn        = t_ctrl.aiNumOn.copy()
    t_out.rPreviousValue = float(getattr(t_ctrl, 'rPreviousValue', 0.0))


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Controller safety wrapper
# ---------------------------------------------------------------------------

_CONTROLLER_TIMEOUT_S = 30  # max seconds allowed for one hourly control call


def _apply_end_hour_buffer_map(
    t_out,
    buffer_map: Optional[Dict[str, Any]],
    *,
    verbose: bool = False,
) -> Dict[str, float]:
    """Populate ``t_out.arBufferN`` slots from end-of-hour values.

    ``buffer_map`` maps buffer slot names (``arBuffer1``..``arBuffer20``) to:
    - string: attribute name on ``t_out``
    - callable: called as ``fn(t_out)``
    - scalar/array-like: direct value (last element used for arrays)

    Returns ``{buffer_name: scalar_value}`` for successfully resolved slots.
    """
    if not buffer_map:
        return {}

    out: Dict[str, float] = {}
    for buf_name, src in buffer_map.items():
        if not isinstance(buf_name, str):
            continue
        if not buf_name.startswith("arBuffer"):
            continue
        try:
            idx = int(buf_name.replace("arBuffer", ""))
        except Exception:
            continue
        if idx < 1 or idx > 20:
            continue

        try:
            if callable(src):
                raw = src(t_out)
            elif isinstance(src, str):
                raw = getattr(t_out, src)
            else:
                raw = src

            arr = np.asarray(raw, dtype=float)
            if arr.size == 0:
                raise ValueError("empty value")
            val = float(arr.ravel()[-1])
            if not np.isfinite(val):
                raise ValueError("non-finite value")

            setattr(t_out, buf_name, val)
            out[buf_name] = val
        except Exception as exc:
            if verbose:
                print(
                    f"  [run] Skipping end-hour buffer map for {buf_name}: {exc}",
                    flush=True,
                )
    return out


def _parse_controller_buffer_alias_map(source_text: str) -> Dict[str, Tuple[str, str]]:
    """Parse `buffer = {'alias': battery.attr|t_out.attr, ...}` alias definitions.

    Returns a mapping of alias key -> (source_name, attribute_name), where
    source_name is either `battery` or `t_out`.
    """
    alias_map: Dict[str, Tuple[str, str]] = {}
    if not source_text:
        return alias_map

    try:
        tree = ast.parse(source_text)
    except Exception:
        return alias_map

    def _consume_dict(node):
        if not isinstance(node, ast.Dict):
            return
        for k_node, v_node in zip(node.keys, node.values):
            if not isinstance(k_node, ast.Constant) or not isinstance(k_node.value, str):
                continue
            if (
                isinstance(v_node, ast.Attribute)
                and isinstance(v_node.value, ast.Name)
                and v_node.value.id in ("battery", "t_out")
            ):
                alias_map[k_node.value] = (v_node.value.id, v_node.attr)

    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for tgt in n.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "buffer":
                    _consume_dict(n.value)
        elif isinstance(n, ast.AnnAssign):
            if isinstance(n.target, ast.Name) and n.target.id == "buffer":
                _consume_dict(n.value)

    return alias_map


def _call_controller_safe(fn, units, battery, t_out, settings, *, num_units, T):
    """Call a user-supplied controller with a wall-clock timeout and return-value
    validation.  Falls back to ``dynamicControl`` on any failure.

    *t_out* is the trimmed controller view (T_ctrl = T – step0 length arrays).
    *T* must equal ``T_ctrl`` (not the full per-hour array length).

    Uses a ``ThreadPoolExecutor`` so the timeout works inside the already-running
    background simulation thread (``signal.alarm`` only works on the main thread).
    """
    import concurrent.futures
    import warnings

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn, units, battery, t_out, settings)
        try:
            result = future.result(timeout=_CONTROLLER_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            future.cancel()
            warnings.warn(
                f"Custom controller timed out after {_CONTROLLER_TIMEOUT_S}s. "
                "Falling back to built-in dynamicControl.",
                RuntimeWarning, stacklevel=4,
            )
            return dynamicControl(units, battery, t_out, settings)
        except Exception as exc:
            warnings.warn(
                f"Custom controller raised an exception: {exc}. "
                "Falling back to built-in dynamicControl.",
                RuntimeWarning, stacklevel=4,
            )
            return dynamicControl(units, battery, t_out, settings)

    # ── Validate the returned tuple ──────────────────────────────────────────
    try:
        ret_units, ret_t_out, ret_battery = result

        # Must return the same number of units
        if len(ret_units) != num_units:
            raise ValueError(
                f"controller returned {len(ret_units)} units, expected {num_units}"
            )
        # Minimal required outputs from a controller:
        #   1) t_out.arTotalElectroDemand: total electrolyser demand [W], length T_ctrl
        #   2) t_out.aiIsOn: per-unit ON/OFF matrix, shape (num_units, T_ctrl)
        #   3) t_out.arProportionPower: per-unit allocation fractions, shape (num_units, T_ctrl)
        for arr_name in ('arTotalElectroDemand', 'aiIsOn', 'arProportionPower'):
            arr = getattr(ret_t_out, arr_name, None)
            if arr is None:
                raise ValueError(f"controller did not set t_out.{arr_name}")
        if len(ret_t_out.arTotalElectroDemand) != T:
            raise ValueError(
                f"t_out.arTotalElectroDemand length {len(ret_t_out.arTotalElectroDemand)}, expected {T}"
            )
        if np.shape(ret_t_out.aiIsOn) != (num_units, T):
            raise ValueError(
                f"t_out.aiIsOn shape {np.shape(ret_t_out.aiIsOn)}, expected ({num_units},{T})"
            )
        if np.shape(ret_t_out.arProportionPower) != (num_units, T):
            raise ValueError(
                f"t_out.arProportionPower shape {np.shape(ret_t_out.arProportionPower)}, expected ({num_units},{T})"
            )

        # NaN / Inf guard on required arrays
        for arr_name in ('arTotalElectroDemand', 'aiIsOn', 'arProportionPower'):
            arr = np.asarray(getattr(ret_t_out, arr_name), dtype=float)
            if np.any(~np.isfinite(arr)):
                raise ValueError(f"t_out.{arr_name} contains NaN or Inf")

        # Derive optional dispatch arrays if custom controller omitted them.
        derived_total_on = np.sum(np.asarray(ret_t_out.aiIsOn), axis=0).astype(float)
        if getattr(ret_t_out, 'arTotalElectroOn', None) is None:
            ret_t_out.arTotalElectroOn = derived_total_on
        else:
            if len(ret_t_out.arTotalElectroOn) != T:
                raise ValueError(
                    f"t_out.arTotalElectroOn length {len(ret_t_out.arTotalElectroOn)}, expected {T}"
                )
            arr_total = np.asarray(ret_t_out.arTotalElectroOn, dtype=float)
            if np.any(~np.isfinite(arr_total)):
                raise ValueError("t_out.arTotalElectroOn contains NaN or Inf")

        arr_prop = np.asarray(ret_t_out.arProportionPower, dtype=float)
        if np.any(~np.isfinite(arr_prop)):
            raise ValueError("t_out.arProportionPower contains NaN or Inf")

        # If battery demand is not provided, infer it for traceability.
        batt_demand = getattr(ret_battery, 'arBatteryDemand', None)
        if batt_demand is None or len(np.asarray(batt_demand).ravel()) != T:
            ref_elec_power = getattr(ret_t_out, 'arElectroAvailablePowerA', None)
            if ref_elec_power is None:
                ref_elec_power = ret_t_out.arTotalElectroDemand
            ret_battery.arBatteryDemand = (
                np.asarray(ret_t_out.arAvailablePower, dtype=float)
                - np.asarray(ref_elec_power, dtype=float)
            )
        else:
            arr_batt = np.asarray(batt_demand, dtype=float)
            if np.any(~np.isfinite(arr_batt)):
                raise ValueError("battery.arBatteryDemand contains NaN or Inf")

    except Exception as val_exc:
        warnings.warn(
            f"Custom controller returned invalid data: {val_exc}. "
            "Falling back to built-in dynamicControl.",
            RuntimeWarning, stacklevel=4,
        )
        return dynamicControl(units, battery, t_out, settings)

    return ret_units, ret_t_out, ret_battery


def _extract_controller_curtailment_command(t_out) -> Any:
    """Extract an optional curtailment command from controller outputs.

    Supported names on ``t_out`` (first match wins):
    - ``arCurtailmentCapMW`` (vector or scalar)
    - ``rCurtailmentCapMW``  (scalar)
    - ``curtailment_cap_mw`` (scalar)
    """
    for attr in ("arCurtailmentCapMW", "rCurtailmentCapMW", "curtailment_cap_mw"):
        raw = getattr(t_out, attr, None)
        if raw is None:
            continue

        scalar = _normalise_curtailment_cap_mw(raw)
        if scalar is not None:
            return scalar

        try:
            arr = np.asarray(raw, dtype=float).ravel()
        except Exception:
            continue
        if arr.size == 0:
            continue

        # Keep shape and order; normalisation is applied per-sample when used.
        return arr

    return None


# Dynamic control (on/off dispatch + proportional sharing)
# ---------------------------------------------------------------------------

def dynamicControl(units, battery, t_out, settings):
    """Built-in dispatch controller.

    Required outputs (for downstream simulation):
    - t_out.arTotalElectroDemand: total electrolyser demand profile [W], length T.
    - t_out.aiIsOn: ON/OFF status matrix, shape (num_units, T).
    - t_out.arProportionPower: per-unit demand fractions, shape (num_units, T).

    """
    # ------------------------------------------------------------------
    # Controller inputs (made explicit for readability)
    # ------------------------------------------------------------------
    # 1) Total incoming power profile [W], length T (includes transient steps).
    #    Downstream logging/output usually ignores the first rTransientSteps.
    total_available_power = np.asarray(t_out.arAvailablePower, dtype=float)
    T = len(total_available_power)

    # 2) Electrolyser ON/OFF matrix [num_units, T].
    #    Seed all timesteps from the previous known state (last column at entry)
    #    so the controller starts from an explicit, fully populated baseline.
    num_units = len(units)
    prev_on = np.asarray(t_out.aiIsOn[:, -1], dtype=int).reshape(num_units, 1)
    t_out.aiIsOn[:, :] = prev_on

    # 3) Total degradation per electrolyser unit, length num_units.
    degradation = np.array([u.rSummedDegradation for u in units], dtype=float)

    # 4) Battery SoC at controller entry.
    initial_soc = np.asarray(battery.arInitialSoC, dtype=float)

    # 5) Battery current usable capacity [J].
    battery_capacity = float(battery.rBatteryRating)

    battery.arBatteryDemand = np.zeros_like(total_available_power)

    # Battery SoC regulator (separate from electrolyser on/off dispatch):
    # this proportional controller pushes SoC toward rControlTargetSoC
    # (default is typically 0.5 unless changed in simulation settings).
    soc_target = battery.rControlTargetSoC   # user-specified target SoC (0–1)
    soc_error = soc_target - initial_soc
    soc_error_abs = np.abs(soc_error)
    # Apply battery correction only when SoC error magnitude exceeds deadband.
    rBatteryProportion = np.where(
        soc_error_abs > 0.1,
        np.clip(soc_error, -(1.0 - soc_target), soc_target),
        0.0,
    )
    # Positive demand charges the battery, negative demand discharges it.
    battery.arBatteryDemand = (
        total_available_power * rBatteryProportion
    ) * battery.rBatteryProportionalGain

    # Battery power-rate and SoC floor protection.
    per_sec_limit = 0.1 * battery_capacity / 3600.0
    battery.arBatteryDemand = np.clip(
        battery.arBatteryDemand, -per_sec_limit, per_sec_limit
    )
    if float(np.atleast_1d(initial_soc).ravel()[-1]) <= 0.0:
        battery.arBatteryDemand = np.clip(battery.arBatteryDemand, 0.0, per_sec_limit)

    t_out.arElectroAvailablePowerA = np.maximum(
        t_out.arAvailablePower - battery.arBatteryDemand, 0.0
    )

    # Exponential smoothing (first-order low-pass)
    tau = 30
    dt = settings.rTimeStep
    alpha = dt / (tau + dt)
    t_out.arElectroAvailablePower = np.zeros_like(t_out.arElectroAvailablePowerA)
    t_out.arElectroAvailablePower[0] = t_out.arElectroAvailablePowerA[0]
    for k in range(1, len(t_out.arElectroAvailablePowerA)):
        t_out.arElectroAvailablePower[k] = (
            alpha * t_out.arElectroAvailablePowerA[k]
            + (1.0 - alpha) * t_out.arElectroAvailablePower[k - 1]
        )
    t_out.rPreviousValue = float(t_out.arElectroAvailablePower[-1])

    rMin   = units[0].rMinPower_s
    rRated = units[0].rRatedPower_s
    arMaxElectroSum = np.floor(t_out.arElectroAvailablePower / rMin).astype(int)
    arMinElectroSum = np.ceil(t_out.arElectroAvailablePower / rRated).astype(int)

    # The transient slots are pre-filled by _prefill_transient_slots before
    # the controller is called.  The dispatch loop therefore starts at k=0
    # (index 0 of the trimmed T_ctrl-length array) and uses k-1=-1 (last
    # element) as the prev-state seed — which is correct because aiIsOn[:,:]
    # and arTotalElectroOn[-1] are pre-seeded in _make_controller_view.
    for k in range(T):
        t_out.aiIsOn[:, k] = t_out.aiIsOn[:, k - 1]
        total_on_prev    = t_out.arTotalElectroOn[k - 1]
        available_power  = t_out.arElectroAvailablePower[k]

        if arMinElectroSum[k] > total_on_prev and available_power > rMin * units[0].rDeadBandRatio:
            rank = np.argsort(degradation)
            need = arMinElectroSum[k] - total_on_prev
            for idx in rank:
                if need <= 0:
                    break
                if t_out.aiIsOn[idx, k] == 0:
                    t_out.aiIsOn[idx, k] = 1
                    t_out.aiNumOn[idx] += 1
                    endi = min(T, k + int(10 * 60 / settings.rTimeStep))
                    t_out.aiWarmedUp[idx, k:endi] = 0
                    units[idx].rTotalTurnOns += 1
                    need -= 1
            t_out.arTotalElectroOn[k] = np.sum(t_out.aiIsOn[:, k])

        elif arMaxElectroSum[k] < total_on_prev:
            rank = np.argsort(degradation)
            need = int(total_on_prev - arMaxElectroSum[k])
            for idx in rank:
                if need <= 0:
                    break
                if t_out.aiIsOn[idx, k] == 1:
                    t_out.aiIsOn[idx, k] = 0
                    need -= 1
            t_out.arTotalElectroOn[k] = np.sum(t_out.aiIsOn[:, k])

        else:
            t_out.arTotalElectroOn[k] = total_on_prev

        if t_out.arTotalElectroOn[k] > 0:
            t_out.arProportionPower[:, k] = (
                t_out.aiIsOn[:, k] / t_out.arTotalElectroOn[k]
            )

    # Required controller output: total demand profile.
    t_out.arTotalElectroDemand = np.clip(
        t_out.arElectroAvailablePower,
        rMin * t_out.arTotalElectroOn,
        rRated * t_out.arTotalElectroOn,
    )
    buffer = {
    "soc": battery.arSoC,
    }
    
    return units, t_out, battery


# ---------------------------------------------------------------------------
# Per-hour electro-thermal coupled step
# ---------------------------------------------------------------------------

def runElectroStackStep1(
    zElectroCell,
    th_banks,
    battery,
    arPowerInput,
    units,
    arTime,
    settings,
    iCntHours,
    t_out_prev,
    cooling_power_feedback: Optional[np.ndarray] = None,
    controller_fn=None,
    curtailment_state: Optional[CurtailmentState] = None,
    curtailment_cap_provider=None,
):
    num_units = units[0].iNumUnits
    T = len(arTime)
    step0  = int(settings.rTransientSteps)
    T_ctrl = T - step0

    cap_source = None
    if curtailment_state is not None and curtailment_state.next_hour_command_mw is not None:
        cap_source = curtailment_state.next_hour_command_mw
    elif curtailment_cap_provider is not None:
        # Backward compatibility for older custom controllers.
        cap_source = curtailment_cap_provider

    # Wind curtailment is applied to raw wind before any downstream processing.
    arWindPowerRaw = np.asarray(np.squeeze(arPowerInput), dtype=float).ravel()
    arWindPowerCurtailed, arCurtailmentRequestedCap, arCurtailmentAppliedCap = (
        _apply_curtailment_on_raw_wind(
            arWindPowerRaw,
            dt=settings.rTimeStep,
            hour_index_abs=int(iCntHours),
            units=units,
            battery=battery,
            settings=settings,
            state=curtailment_state,
            cap_provider=cap_source,
            transient_steps=step0,
        )
    )

    # Wind → available power (filtered)
    arWindPowerFilt = lsim_first_order_lowpass(arWindPowerCurtailed, arTime, 1.0)
    arAvailablePower = arWindPowerFilt - units[0].rAncillaryPower_s * num_units
    if cooling_power_feedback is not None:
        cool_arr = np.asarray(cooling_power_feedback, dtype=float).ravel()
        if cool_arr.shape[0] != arAvailablePower.shape[0]:
            raise ValueError("cooling_power_feedback length must match time axis")
        arAvailablePower = np.maximum(arAvailablePower - cool_arr, 0.0)

    bank_to_units, unit_to_bank = _make_bank_index_maps(units)
    num_banks_total = len(bank_to_units)

    # ── Normalise th_banks to a list ────────────────────────────────────────
    if isinstance(th_banks, BaseBankThermal):
        th_banks = [copy.deepcopy(th_banks) for _ in range(num_banks_total)]
    elif not isinstance(th_banks, list):
        th_template = bank_thermal_from_kind("PEM", units[0])
        th_banks = [copy.deepcopy(th_template) for _ in range(num_banks_total)]
    elif len(th_banks) < num_banks_total:
        th_banks = th_banks + [copy.deepcopy(th_banks[0])
                                for _ in range(num_banks_total - len(th_banks))]

    # Per-bank cell state copies (carry rT for curve rebuilds).
    # Shallow copy is sufficient: only the scalar .rT attribute is set on each
    # copy; the underlying numpy arrays are read-only here and never mutated.
    ec_state_banks = [copy.copy(zElectroCell) for _ in range(num_banks_total)]
    bank_units = [[units[j] for j in idxs] for idxs in bank_to_units]
    last_T_bank = np.full(num_banks_total, np.nan)
    last_deg_unit = np.full(num_units, np.nan)
    bank_ec_curves: List[Optional[Any]] = [None] * num_banks_total
    temp_cache_threshold = 0.1

    cache_arP   = [None] * num_units
    cache_arI   = [None] * num_units
    cache_arVsd = [None] * num_units
    cache_arVs  = [None] * num_units
    cache_arH2  = [None] * num_units
    # Precomputed segment slopes for scalar interpolation (built in curve-rebuild block)
    cache_slope_PI   = [None] * num_units  # slopes for P → I lookup
    cache_slope_IVsd = [None] * num_units  # slopes for I → Vsd lookup
    cache_slope_IVs  = [None] * num_units  # slopes for I → Vs lookup
    cache_slope_IH2  = [None] * num_units  # slopes for I → H2 lookup
    cache_nseg       = [0]    * num_units  # max valid segment index (len(arI) - 2)

    # ── Transient bookkeeping (computed once, used by helpers + physics loop) ─

    # ── Initialise output struct ─────────────────────────────────────────────
    aiIsOn = np.zeros((num_units, T), dtype=int)

    t_out = TimeOutputs()
    t_out.arTime                    = arTime
    t_out.arWindPowerRaw            = arWindPowerRaw
    t_out.arWindPowerCurtailed      = arWindPowerCurtailed
    t_out.arCurtailmentRequestedCap = arCurtailmentRequestedCap
    t_out.arCurtailmentAppliedCap   = arCurtailmentAppliedCap
    t_out.arWindPowerFilt           = arWindPowerFilt
    t_out.arAvailablePower          = arAvailablePower
    t_out.arElectroAvailablePowerA  = np.zeros_like(arAvailablePower)
    t_out.arElectroAvailablePower   = np.zeros_like(arAvailablePower)
    t_out.rPreviousValue            = 0.0
    t_out.arTotalElectroDemand      = np.zeros_like(arAvailablePower)
    t_out.arProportionPower         = np.zeros((num_units, T))
    t_out.aiIsOn                    = aiIsOn
    t_out.aiWarmedUp                = np.ones((num_units, T), dtype=int)
    t_out.aiNumOn                   = np.zeros(num_units, dtype=int)
    t_out.arTotalElectroOn          = np.zeros(T)
    t_out.arElectroDemand           = np.zeros((num_units, T))
    t_out.arI_unit                  = np.zeros((num_units, T))
    t_out.arV_unit                  = np.zeros((num_units, T))
    t_out.arV_unitUseful            = np.zeros((num_units, T))
    t_out.arPower_unit              = np.zeros((num_units, T))
    t_out.arPower_unitUseful        = np.zeros((num_units, T))
    t_out.arDegradationInEfficiency = np.zeros((num_units, T))
    t_out.arV_cell                  = np.zeros((num_units, T))
    t_out.arProducedH2Dot           = np.zeros((num_units, T_ctrl + 1))
    t_out.arHydroEfficiency         = np.zeros((num_units, T))
    t_out.arP_el_total              = np.zeros(T)
    t_out.arT_stack                 = np.zeros(T)
    t_out.arH2Dot_total             = np.zeros(T)
    t_out.arV_cell_avg              = np.zeros(T)
    t_out.arEta_el_total            = np.zeros(T)
    t_out.arEta_system_total        = np.zeros(T)
    t_out.arP_el_banks              = np.zeros((num_banks_total, T))
    t_out.arT_banks                 = np.zeros((num_banks_total, T))
    t_out.arP_el_unit               = np.zeros((num_units, T))
    t_out.arQ_gain_unit             = np.zeros((num_units, T))
    t_out.arVtn_unit                = np.zeros((num_units, T))
    t_out.arT_unit_bank             = np.zeros((num_units, T))
    t_out.arQ_gain_banks            = np.zeros((num_banks_total, T))
    t_out.arQ_lost_banks            = np.zeros((num_banks_total, T))
    t_out.arQ_cool_banks            = np.zeros((num_banks_total, T))
    t_out.arP_cool_elec_banks       = np.zeros((num_banks_total, T))
    t_out.arG_eq_banks              = np.zeros((num_banks_total, T))
    t_out.arC_th_banks              = np.zeros((num_banks_total, T))
    t_out.arQ_gain_total            = np.zeros(T)
    t_out.arQ_lost_total            = np.zeros(T)
    t_out.arQ_cool_total            = np.zeros(T)
    t_out.arP_cool_elec_total       = np.zeros(T)
    t_out.aiIsOn_clean                 = np.zeros((num_units, T), dtype=int)
    t_out.aiAssignmentError              = np.zeros(T, dtype=int)

    # ── Pre-fill transient slots from previous hour ──────────────────────────
    # Fills the first step0 timesteps of dispatch arrays with the last sample
    # of the previous hour, so the warm-up window holds meaningful data rather
    # than zeros.  Also seeds all columns of aiIsOn with the previous on/off
    # state so both controllers start from a fully-populated baseline.
    if t_out_prev is not None:
        _prefill_transient_slots(t_out, t_out_prev, step0)

    # ── Build trimmed controller view (non-transient region only) ────────────
    # Both the built-in dynamicControl and any custom controller receive a
    # T_ctrl-length copy of t_out.  The dispatch loop therefore runs from k=0
    # with no knowledge of the warm-up period.
    t_out_ctrl = _make_controller_view(t_out, step0, T_ctrl, num_units)

    # ── Dynamic control pass ─────────────────────────────────────────────────
    if controller_fn is not None:
        units, t_out_ctrl, battery = _call_controller_safe(
            controller_fn, units, battery, t_out_ctrl, settings,
            num_units=num_units, T=T_ctrl,
        )
        if curtailment_state is not None:
            curtailment_state.next_hour_command_mw = _extract_controller_curtailment_command(t_out_ctrl)
    else:
        units, t_out_ctrl, battery = dynamicControl(units, battery, t_out_ctrl, settings)

    # ── Merge trimmed outputs back into the full T-length t_out ──────────────
    _merge_controller_outputs(t_out, t_out_ctrl, step0)

    min_power = float(units[0].rMinPower_s)
    rated_power_curve = float(units[0].rRatedPower_s)
    rated_power = rated_power_curve

    # Assume aiIsOn is a NumPy array of shape (num_units, T)

    # Identify invalid entries (anything not 0 or 1)
    invalid_mask = (aiIsOn != 0) & (aiIsOn != 1)

    # Count how many invalid values per time step (column-wise)
    t_out.aiAssignmentError = np.sum(invalid_mask, axis=0)

    # Replace invalid values with 0
    t_out.aiIsOn_clean = np.where(invalid_mask, 0, aiIsOn)

    # Sum across units for each time step
    t_out.arTotalElectroOn = np.sum(t_out.aiIsOn_clean, axis=0)

    # Use at least nominal nameplate power for guard limits. The electro-curve
    # endpoint can be lower than nameplate and otherwise caps default models
    # below expected plant rating (for example ~19.5 MW instead of ~25 MW).
    try:
        i_rated = float(zElectroCell.rI_rated) * float(zElectroCell.rA_cell)
        v_cell_nom = float(zElectroCell.rV_cellNom)
        if units[0].iControlLevel == 1:
            n_cells = float(units[0].iN_cell * units[0].iN_stacks * units[0].iN_banks)
        elif units[0].iControlLevel == 2:
            n_cells = float(units[0].iN_cell * units[0].iN_stacks)
        else:
            n_cells = float(units[0].iN_cell)
        rated_power_nominal = v_cell_nom * i_rated * n_cells
        if np.isfinite(rated_power_nominal) and rated_power_nominal > 0.0:
            rated_power = max(rated_power_curve, rated_power_nominal)
    except Exception:
        # Fall back to curve-derived rating if nominal metadata is unavailable.
        rated_power = rated_power_curve
    # Controllers are expected to provide arTotalElectroDemand and aiIsOn.
    # Keep a backward-compatible fallback for older custom controllers.
    demand_in = getattr(t_out, 'arTotalElectroDemand', None)
    if demand_in is None or len(np.asarray(demand_in).ravel()) != T:
        demand_in = getattr(t_out, 'arElectroAvailablePower', None)
        if demand_in is None or len(np.asarray(demand_in).ravel()) != T:
            raise ValueError(
                "Controller must set t_out.arTotalElectroDemand (length T)."
            )
    t_out.arTotalElectroDemand = np.clip(
        np.asarray(demand_in, dtype=float),
        min_power * t_out.arTotalElectroOn,
        rated_power * t_out.arTotalElectroOn,
    )
    for i in range(num_units):
        t_out.arElectroDemand[i, :] = np.minimum(
            rated_power,
            t_out.arProportionPower[i, :] * t_out.arTotalElectroDemand,
        )

    def _bounded_allocate(total_power, weights, lo, hi):
        """Allocate total_power across channels with elementwise [lo, hi] bounds.

        Returns an array that sums (approximately) to total_power when feasible.
        """
        n = len(weights)
        if n == 0:
            return np.zeros(0, dtype=float)

        total_power = float(total_power)
        lo = np.asarray(lo, dtype=float)
        hi = np.asarray(hi, dtype=float)
        w = np.asarray(weights, dtype=float)
        w = np.clip(w, 0.0, np.inf)

        if np.sum(w) <= 0.0:
            w = np.ones(n, dtype=float)

        # Start from weighted allocation, then project into bounds.
        x = total_power * (w / np.sum(w))
        x = np.clip(x, lo, hi)

        for _ in range(30):
            rem = total_power - float(np.sum(x))
            if abs(rem) <= 1e-9:
                break

            if rem > 0.0:
                free = x < (hi - 1e-12)
                if not np.any(free):
                    break
                wf = w[free]
                if np.sum(wf) <= 0.0:
                    wf = np.ones(np.sum(free), dtype=float)
                add = rem * (wf / np.sum(wf))
                x[free] = np.minimum(hi[free], x[free] + add)
            else:
                free = x > (lo + 1e-12)
                if not np.any(free):
                    break
                wf = w[free]
                if np.sum(wf) <= 0.0:
                    wf = np.ones(np.sum(free), dtype=float)
                sub = (-rem) * (wf / np.sum(wf))
                x[free] = np.maximum(lo[free], x[free] - sub)

        return x

    # ── Post-controller physical guards (outside controller code) ───────────
    # Enforce individual per-unit min/max on ON units by adjusting ON count and
    # re-allocating demand with bounds. This applies to both built-in and custom
    # controllers.
    for k in range(T):
        total_k = float(t_out.arTotalElectroDemand[k])
        pref_all = np.asarray(t_out.arProportionPower[:, k], dtype=float).copy()
        on_idx = np.flatnonzero(t_out.aiIsOn[:, k] > 0)
        n_on = int(len(on_idx))

        t_out.arElectroDemand[:, k] = 0.0
        t_out.arProportionPower[:, k] = 0.0

        if n_on <= 0 or total_k <= 0.0:
            t_out.arTotalElectroDemand[k] = 0.0
            t_out.arTotalElectroOn[k] = 0.0
            continue

        if min_power > 0.0:
            n_max_by_min = int(np.floor(total_k / min_power))
            n_keep = max(0, min(n_on, n_max_by_min))
        else:
            n_keep = n_on

        # If too many units are ON to satisfy individual minimum power,
        # turn OFF the lowest-priority units based on controller proportions.
        if n_keep < n_on:
            sort_key = np.argsort(pref_all[on_idx])
            off_count = n_on - n_keep
            off_idx = on_idx[sort_key[:off_count]]
            t_out.aiIsOn[off_idx, k] = 0
            on_idx = np.flatnonzero(t_out.aiIsOn[:, k] > 0)
            n_on = int(len(on_idx))

        t_out.arTotalElectroOn[k] = float(n_on)
        if n_on <= 0:
            t_out.arTotalElectroDemand[k] = 0.0
            continue

        # Re-allocate demand to ON units with individual bounds.
        pref = pref_all[on_idx]
        lo = np.full(n_on, min_power, dtype=float)
        hi = np.full(n_on, rated_power, dtype=float)
        alloc = _bounded_allocate(total_k, pref, lo, hi)
        total_alloc = float(np.sum(alloc))
        t_out.arTotalElectroDemand[k] = total_alloc

        t_out.arElectroDemand[on_idx, k] = alloc
        if total_alloc > 0.0:
            t_out.arProportionPower[on_idx, k] = alloc / total_alloc
        else:
            t_out.arProportionPower[on_idx, k] = 1.0 / float(n_on)

    T_amb_hour = 15.0
    dt = settings.rTimeStep
    H2_LHV = 119_988.0  # J/g

    arH2Dot_time = np.zeros((num_units, T))

    # Pre-allocate bank accumulator work arrays so they are zeroed in-place
    # each second rather than re-allocated (avoids GC pressure over millions of steps).
    _P_bank_work     = np.zeros(num_banks_total)
    _H2_bank_work    = np.zeros(num_banks_total)
    _Qgain_bank_work = np.zeros(num_banks_total)

    # Pre-cache per-unit constants that are fixed for the entire per-second loop.
    # Avoids repeated Python attribute lookups (units[i].x) on every second × unit.
    _unit_bank_idx = [int(unit_to_bank[i]) for i in range(num_units)]
    _unit_rampup   = [float(units[i].rRampUp_W_s)  if np.isfinite(units[i].rRampUp_W_s)  else 1e99
                      for i in range(num_units)]
    _unit_rampdown = [float(units[i].rRampDown_W_s) if np.isfinite(units[i].rRampDown_W_s) else 1e99
                      for i in range(num_units)]
    _unit_vtn = np.zeros(num_units, dtype=float)
    for _i in range(num_units):
        _el = units[_i]
        if _el.iControlLevel == 3:
            _unit_vtn[_i] = V_TN_CELL * _el.iN_cell
        elif _el.iControlLevel == 2:
            _unit_vtn[_i] = V_TN_CELL * _el.iN_cell * _el.iN_stacks
        else:
            _unit_vtn[_i] = V_TN_CELL * _el.iN_cell * _el.iN_stacks * _el.iN_banks
    _divisor_inv = 1.0 / max(settings.rDivisor, 1.0)

    # ── Per-second coupled loop ──────────────────────────────────────────────
    for k in range(step0, T):

        # 1) Rebuild curves per bank if temperature changed enough
        for b, idxs in enumerate(bank_to_units):
            T_b = th_banks[b].rT
            deg_changed = any(
                not np.isfinite(last_deg_unit[j]) or
                abs(units[j].rSummedDegradation - last_deg_unit[j]) > 1e-12
                for j in idxs
            )
            if (not np.isfinite(last_T_bank[b]) or
                    abs(T_b - last_T_bank[b]) > temp_cache_threshold or
                    deg_changed):
                ec_state_banks[b].rT = T_b
                ec_curves_b = ec_state_banks[b].build_curves()
                bank_ec_curves[b] = ec_curves_b
                last_T_bank[b] = T_b

                rLHV_H2  = 119_988.0
                rMu      = 2.01588
                rF_const = 9.6485e4
                rN_const = 2
                rLossDry = 0.03
                rConstantPart = rMu / rF_const / rN_const * (1 - rLossDry)

                arJ   = ec_curves_b.arCurrentDensity
                arFEff = ec_curves_b.faraday_efficiency(arJ)
                arVc   = ec_curves_b.arV_cell
                rA     = ec_curves_b.rA_cell

                for j in idxs:
                    e = units[j]
                    if e.iControlLevel == 1:
                        arV_s  = arVc * e.iN_cell * e.iN_stacks * e.iN_banks
                        arV_sd = (arVc + e.rSummedDegradation) * e.iN_cell * e.iN_stacks * e.iN_banks
                        arI_s  = arJ * rA
                        arH2   = arFEff * rConstantPart * arI_s * e.iN_cell * e.iN_stacks * e.iN_banks
                    elif e.iControlLevel == 2:
                        arV_s  = arVc * e.iN_cell * e.iN_stacks
                        arV_sd = (arVc + e.rSummedDegradation) * e.iN_cell * e.iN_stacks
                        arI_s  = arJ * rA
                        arH2   = arFEff * rConstantPart * arI_s * e.iN_cell * e.iN_stacks
                    else:
                        arV_s  = arVc * e.iN_cell
                        arV_sd = (arVc + e.rSummedDegradation) * e.iN_cell
                        arI_s  = arJ * rA
                        arH2   = arFEff * rConstantPart * arI_s * e.iN_cell

                    arP = arI_s * arV_sd
                    cache_arP[j]   = arP
                    cache_arI[j]   = arI_s
                    cache_arVsd[j] = arV_sd
                    cache_arVs[j]  = arV_s
                    cache_arH2[j]  = arH2
                    # Precompute per-segment slopes so the inner loop uses one
                    # searchsorted instead of three separate np.interp calls.
                    _dP_seg = np.diff(arP).copy()
                    _dP_seg[_dP_seg == 0.0] = 1.0
                    cache_slope_PI[j]   = np.diff(arI_s)  / _dP_seg
                    _dI_seg = np.diff(arI_s).copy()
                    _dI_seg[_dI_seg == 0.0] = 1.0
                    cache_slope_IVsd[j] = np.diff(arV_sd) / _dI_seg
                    cache_slope_IVs[j]  = np.diff(arV_s)  / _dI_seg
                    cache_slope_IH2[j]  = np.diff(arH2)   / _dI_seg
                    cache_nseg[j]       = len(arI_s) - 2
                    last_deg_unit[j] = units[j].rSummedDegradation

        # 2) Interpolate per-unit quantities + accumulate per-bank heat
        _P_bank_work[:]     = 0.0
        _H2_bank_work[:]    = 0.0
        _Qgain_bank_work[:] = 0.0
        P_bank_k     = _P_bank_work
        H2_bank_k    = _H2_bank_work
        Qgain_bank_k = _Qgain_bank_work

        for i in range(num_units):
            on = t_out.aiIsOn[i, k]
            b  = _unit_bank_idx[i]

            # Fast path: OFF units produce nothing.  Arrays are zero-initialised;
            # only bank temperature needs recording before skipping ahead.
            if on == 0:
                t_out.arElectroDemand[i, k] = 0.0
                t_out.arT_unit_bank[i, k]   = th_banks[b].rT
                continue

            demand_target = float(t_out.arElectroDemand[i, k])
            prev_P = float(t_out.arPower_unit[i, k - 1]) if k > 0 else 0.0

            demand_ik = float(np.clip(
                demand_target,
                prev_P - _unit_rampdown[i] * dt,
                prev_P + _unit_rampup[i]   * dt,
            ))
            demand_ik = float(np.clip(demand_ik, min_power, rated_power))
            t_out.arElectroDemand[i, k] = demand_ik

            # P → I: keep np.interp for correct boundary clamping
            I_ik = float(np.interp(demand_ik, cache_arP[i], cache_arI[i]))

            # I → Vsd, Vs, H2: one shared searchsorted replaces three np.interp calls
            _cI = cache_arI[i]
            _ii = min(max(int(np.searchsorted(_cI, I_ik, side='right')) - 1, 0), cache_nseg[i])
            _dI = I_ik - _cI[_ii]
            V_deg = float(cache_arVsd[i][_ii] + cache_slope_IVsd[i][_ii] * _dI)
            V_use = float(cache_arVs[i][_ii]  + cache_slope_IVs[i][_ii]  * _dI)
            arH2Dot_time[i, k] = float(cache_arH2[i][_ii] + cache_slope_IH2[i][_ii] * _dI)

            t_out.arI_unit[i, k]       = I_ik
            t_out.arV_unit[i, k]       = V_deg
            t_out.arV_unitUseful[i, k] = V_use

            P_deg = I_ik * V_deg
            P_use = I_ik * V_use
            t_out.arPower_unit[i, k]       = P_deg
            t_out.arPower_unitUseful[i, k] = P_use
            t_out.arP_el_unit[i, k]        = P_deg

            t_out.arDegradationInEfficiency[i, k] = 0.0 if demand_ik == 0.0 else 1.0 - P_use / demand_ik
            t_out.arV_cell[i, k] = V_deg * _divisor_inv

            Vtn_equiv = _unit_vtn[i]
            q_gain_i  = I_ik * max(V_deg - Vtn_equiv, 0.0)
            t_out.arVtn_unit[i, k]    = Vtn_equiv
            t_out.arQ_gain_unit[i, k] = q_gain_i

            P_bank_k[b]     += P_deg
            H2_bank_k[b]    += arH2Dot_time[i, k]
            Qgain_bank_k[b] += q_gain_i
            t_out.arT_unit_bank[i, k] = th_banks[b].rT

        # 3) Global totals and bank thermal advance
        P_el_k   = float(np.sum(P_bank_k))
        H2dot_k  = float(np.sum(H2_bank_k))
        Vcell_avg_k = (float(np.mean(t_out.arV_cell[:, k]))
                       if t_out.arTotalElectroOn[k] > 0 else 0.0)
        eta_stack_k = 0.0 if P_el_k <= 0.0 else (H2_LHV * H2dot_k) / P_el_k

        Qg_sum = Ql_sum = Qc_sum = Pc_sum = 0.0
        for b in range(num_banks_total):
            diag: dict = {}
            th_banks[b] = thermal_step(
                th_banks[b], Q_el=float(Qgain_bank_k[b]),
                T_amb=T_amb_hour, dt=dt, out=diag,
            )
            t_out.arP_el_banks[b, k] = P_bank_k[b]
            t_out.arT_banks[b, k]    = th_banks[b].rT
            t_out.arQ_gain_banks[b, k]      = diag.get("q_gain", 0.0)
            t_out.arQ_lost_banks[b, k]      = diag.get("q_lost", 0.0)
            t_out.arQ_cool_banks[b, k]      = diag.get("q_cool", 0.0)
            t_out.arP_cool_elec_banks[b, k] = diag.get("p_cool_elec", 0.0)
            t_out.arG_eq_banks[b, k]        = diag.get("G_eq", 0.0)
            t_out.arC_th_banks[b, k]        = diag.get("C_th", 0.0)
            Qg_sum += t_out.arQ_gain_banks[b, k]
            Ql_sum += t_out.arQ_lost_banks[b, k]
            Qc_sum += t_out.arQ_cool_banks[b, k]
            Pc_sum += t_out.arP_cool_elec_banks[b, k]

        t_out.arQ_gain_total[k]      = Qg_sum
        t_out.arQ_lost_total[k]      = Ql_sum
        t_out.arQ_cool_total[k]      = Qc_sum
        t_out.arP_cool_elec_total[k] = Pc_sum

        total_power_w_cool  = P_el_k + Pc_sum
        eta_system_k = (0.0 if total_power_w_cool <= 0.0
                        else (H2_LHV * H2dot_k) / total_power_w_cool)

        t_out.arP_el_total[k]       = P_el_k
        t_out.arH2Dot_total[k]      = H2dot_k
        t_out.arV_cell_avg[k]       = Vcell_avg_k
        t_out.arEta_el_total[k]     = eta_stack_k
        t_out.arEta_system_total[k] = eta_system_k
        t_out.arT_stack[k]          = float(np.mean(t_out.arT_banks[:, k]))

    # ── Post-loop: degradation + H2 outputs ─────────────────────────────────
    for i in range(num_units):
        seg_v = t_out.arV_cell[i, step0 - 1:] * t_out.aiIsOn[i, step0 - 1:]
        c_s = float(np.sum(seg_v) * dt)

        c_f = _rainflow_fatigue_sum(seg_v) if np.any(seg_v != 0.0) else 0.0

        if not hasattr(units[i], "arDegradationSteady") or units[i].arDegradationSteady is None:
            units[i].arDegradationSteady  = []
            units[i].arDegradationFatigue = []
            units[i].arDegradationOnOff   = []

        units[i].arDegradationSteady.append(units[i].r_s * c_s)
        units[i].arDegradationFatigue.append(units[i].r_f * c_f)
        units[i].arDegradationOnOff.append(0.0)

        t_out.arProducedH2Dot[i, :] = arH2Dot_time[i, step0 - 1:]

        with np.errstate(divide="ignore", invalid="ignore"):
            eff_vec = np.divide(
                t_out.arPower_unitUseful[i, :],
                t_out.arElectroDemand[i, :],
                out=np.zeros_like(t_out.arElectroDemand[i, :]),
                where=t_out.arElectroDemand[i, :] != 0.0,
            )
        t_out.arDegradationInEfficiency[i, :] = 1.0 - eff_vec
        t_out.arHydroEfficiency[i, :] = np.interp(
            t_out.arElectroDemand[i, :],
            units[i].arP_Total_s,
            units[i].arEfficiency_s,
        )

        units[i].arDegradationOnOff[-1]  = units[i].r_o * float(t_out.aiNumOn[i])
        units[i].rDegradationOnOffTotal  = float(np.sum(units[i].arDegradationOnOff))
        units[i].rDegradationSteadyTotal = float(np.sum(units[i].arDegradationSteady))
        units[i].rDegradationFatigueTotal = float(np.sum(units[i].arDegradationFatigue))

        total_this_hour = (units[i].arDegradationSteady[-1]
                           + units[i].arDegradationFatigue[-1]
                           + units[i].arDegradationOnOff[-1])
        units[i].rSummedDegradation += float(total_this_hour)

    t_out.arTotalElectroDemand = (
        np.sum(t_out.arElectroDemand, axis=0)
        + units[0].rAncillaryPower_s * num_units
    )

    return units, t_out, battery, th_banks


# ---------------------------------------------------------------------------
# Battery model (per-hour)
# ---------------------------------------------------------------------------

def runBattery1(t_out, battery, settings) -> "Battery":
    """Update battery SoC and degradation for one hour."""
    start = int(settings.rTransientSteps) - 1
    P_batt = t_out.arAvailablePower[start:] - t_out.arTotalElectroDemand[start:]

    dt = settings.rTimeStep
    SoC = np.empty_like(P_batt)
    effective_power = np.zeros_like(P_batt)
    soc_prev = float(np.clip(battery.arInitialSoC, 0.0, 1.0))
    for k in range(len(P_batt)):
        if battery.rBatteryRating <= 0.0 or dt <= 0.0:
            delta = 0.0
        else:
            delta = float(np.clip(
                P_batt[k] * dt / battery.rBatteryRating, -soc_prev, 1.0 - soc_prev
            ))
        soc_curr = soc_prev + delta
        SoC[k] = soc_curr
        effective_power[k] = 0.0 if dt <= 0.0 else delta * battery.rBatteryRating / dt
        soc_prev = soc_curr

    battery.arSoC          = SoC
    battery.arDoD          = 1.0 - SoC
    battery.arBatteryPower = effective_power
    battery.arSpillPower  = np.zeros_like(P_batt)
    battery.arSpillPower[:] = P_batt - effective_power

    cycles = rainflow(battery.arSoC, dt=dt)

    battery.rSocAv  = float(np.mean(SoC))
    battery.rSocMax = float(np.max(SoC))
    battery.rSocMin = float(np.min(SoC))
    battery.rDodAv  = float(np.mean(battery.arDoD))

    rStAv = battery.rKt * (settings.rTotalTime - settings.rTransientSteps * settings.rTimeStep + 1)
    rSsAv = np.exp(battery.rKs * (battery.rSocAv - battery.rSoCRef))
    battery.rFt += rStAv * rSsAv

    # Vectorised cycle accumulation (replaces per-cycle Python loop; ~60x faster)
    if cycles.shape[0]:
        Ci    = cycles[:, 0]
        DoD   = cycles[:, 1] * 2.0
        SoC_m = cycles[:, 2]
        Sd    = 1.0 / (battery.rKd1 * (DoD ** battery.rKd2) + battery.rKd3)
        Ss    = np.exp(battery.rKs * (SoC_m - battery.rSoCRef))
        battery.rFc += float(np.sum(Ci * Sd * Ss))

    rFd = battery.rFc + battery.rFt
    battery.rRCD = (battery.rAlphaSei * np.exp(-battery.rBetaSei * rFd)
                    + (1.0 - battery.rAlphaSei) * np.exp(-rFd))

    battery.rFc = 0.0
    battery.rFt = 0.0

    battery.rBatteryRating = battery.rBatteryRating * battery.rRCD

    if battery.rBatteryRating < battery.rReplacementThreshold * battery.rInitialBatteryRating:
        battery.rBatteryRating = battery.rInitialBatteryRating
        battery.iNumReplacements += 1

    battery.arInitialSoC = float(battery.arSoC[-1])
    return battery


###############################################################################################################


# R2H2 main class
class R2H2():
    # Gather simulation parameters from system environment variables
    
    def _get_allowed_classes(self):
        """Dynamically gather allowed classes from initialized components."""
        return [type(getattr(self, attr)).__name__ for attr in dir(self) 
                if hasattr(self, attr) and hasattr(getattr(self, attr), '__class__') 
                and not attr.startswith('_') and attr not in ['paths', 'simulation_name', 'verbose']]
    
    def _safe_instantiate_component(self, class_name):
        """Safely instantiate a component class from built-in defaults.

        Args:
            class_name (str): The name of the component class.

        Returns:
            object: An instance of the component class initialised from defaults.

        Raises:
            ValueError: If the class name is not valid or allowed.
        """
        # Get allowed classes from components module's __all__
        import r2h2.components as components_module
        
        # Use __all__ from components module as the allowed classes
        allowed_classes = getattr(components_module, '__all__', [])
        
        # Fallback to dynamic detection if __all__ is empty
        if not allowed_classes:
            try:
                allowed_classes = self._get_allowed_classes()
            except:
                # Final fallback list
                allowed_classes = ['Simulation', 'Battery', 'ElectroCellPEM', 'ElectrolyserUnit', 
                                 'ThermalProperties', 'TimeOutputs', 'WindInputs']
        
        # Validate class name is allowed
        if class_name not in allowed_classes:
            raise ValueError(f"Invalid class name: {class_name}. Allowed classes: {allowed_classes}")
        
        # Try to get the class from components module first
        if hasattr(components_module, class_name):
            component_class = getattr(components_module, class_name)
            return component_class()
        
        # Fallback: try current module (globals)
        if class_name in globals():
            component_class = globals()[class_name]
            return component_class()
        
        raise ValueError(f"Class '{class_name}' not found in components module or current namespace")
    
    def __init__(self, simulation_name=None, verbose=False,
                 kind: str = "PEM",
                 use_cooling_feedback: bool = False,
                 insulated: bool = False,
                 wind_h5_path: Optional[str] = None):
        """
        Initialise an R2H2 simulation.

        Args:
            simulation_name: Django ORM ``Simulation`` model instance (optional).
                             When provided, ``run()`` will load component values
                             from the DB via ``map_to_db_objects()``.
            verbose:         Print progress messages.
            kind:            Technology preset – currently only ``"PEM"``.
            use_cooling_feedback: Two-pass cooling feedback in the hourly loop.
            insulated:       Start thermal banks in insulated mode.
            wind_h5_path:    Path to a wind HDF5 file.  When given the wind data
                             is loaded immediately and stored on ``self.windinputs``.
        """
        self.simulation_name = simulation_name
        self.verbose = verbose
        self.kind = kind
        self.use_cooling_feedback = use_cooling_feedback
        self.insulated = insulated

        self.paths = Paths(verbose=self.verbose)
        self.simulation = Simulation()

        # ── Build component instances from built-in defaults ─────────────────
        # DB values are applied later via map_to_db_objects() when
        # simulation_name (a Django Simulation instance) is provided.
        _COMPONENT_CLASSES = [
            'Battery', 'ElectroCellPEM', 'ElectrolyserUnit',
            'ThermalProperties', 'TimeOutputs', 'WindInputs',
        ]
        for class_name in _COMPONENT_CLASSES:
            component_instance = self._safe_instantiate_component(class_name)
            setattr(self, class_name.lower(), component_instance)

        # ── Apply technology topology + dynamics to ElectrolyserUnit ─────────
        el = self.electrolyserunit
        el = apply_unit_topology(self.kind, el)
        el.iControlLevel = getattr(el, 'iControlLevel', 2)   # default: bank-level

        if el.iControlLevel == 1:
            el.iNumUnits = el.iNumElectro
            self.simulation.rDivisor = el.iN_banks * el.iN_stacks * el.iN_cell
        elif el.iControlLevel == 2:
            el.iNumUnits = el.iNumElectro * el.iN_banks
            self.simulation.rDivisor = el.iN_stacks * el.iN_cell
        else:
            el.iNumUnits = el.iNumElectro * el.iN_banks * el.iN_stacks
            self.simulation.rDivisor = el.iN_cell

        el = apply_unit_profile(self.kind, el)
        self.electrolyserunit = el

        # ── Size battery  (MWh → J, derive proportional gain) ────────────────
        bat = self.battery
        bat.rInitialBatteryRating   = bat.rBatteryMWh * 3.6e9
        bat.rBatteryRating          = bat.rInitialBatteryRating
        bat.rBatteryProportionalGain = bat.rInitialBatteryRating / 3600.0 / 10e6
        self.battery = bat

        # ── Optionally load wind data from HDF5 ──────────────────────────────
        if wind_h5_path is not None:
            self.windinputs = load_wind_h5(wind_h5_path)


    # ---  GENERIC UPDATE FUNCTION TO RELOAD A COMPONENT FROM THE DATABASE  --- #

    def update_component(self, class_name=None, component_name=None):
        """Reload a component instance from the database.

        Args:
            class_name (str):     Component class name (e.g. ``'Battery'``).
            component_name (str): DB record name (e.g. ``'Main Battery'``).

        Usage examples::

            sim.update_component(class_name='Battery', component_name='Main Battery')
            sim.update_component(class_name='ElectrolyserUnit', component_name='Main Electrolyser Unit')
        """
        if class_name is None:
            raise KeyError("Please provide a class name to update (e.g. 'Battery').")
        if component_name is None:
            raise KeyError("Please provide a component name to update (e.g. 'Main Battery').")

        # Start from built-in defaults then overwrite from DB
        component_instance = self._safe_instantiate_component(class_name)

        # Attempt to pull values from the Django DB record with this name
        try:
            import r2h2.components as _cm
            import django.apps
            model_cls = None
            for app_config in django.apps.apps.get_app_configs():
                try:
                    model_cls = app_config.get_model(class_name)
                    break
                except LookupError:
                    continue
            if model_cls is not None:
                db_obj = model_cls.objects.get(name=component_name)
                for field in db_obj._meta.get_fields():
                    fname = field.name
                    if hasattr(component_instance, fname):
                        setattr(component_instance, fname, getattr(db_obj, fname))
        except Exception:
            pass  # DB not available or record not found — keep defaults

        setattr(self, class_name.lower(), component_instance)

    


    #######################################################################################
    ###  UI-APP IMPLEMENTATION  ###########################################################
    #######################################################################################
    # WIP - not yet implemented
    def electrolyser(self):
        """
        Compute per-unit arrays for voltage, current, H2 rate, power, efficiency.
        Mirrors electrolyser() logic with iControlLevel branches.
        """
        # Constants
        # rHhwH2 = 141876.0  # J/g (higher heating value), as used in doc
        rLHV_H2 = 119_988.0  # J/g (33.33 kWh/kg)
        
        rMu = 2.01588      # g/mol
        rF = 9.6485E4      # sA/mol
        rN = 2
        rLossDry = 0.03
        rConstantPart = rMu/rF/rN*(1-rLossDry)

        arCurrentDensity = self.electrocellpem.arCurrentDensity
        arFaradayEff = self.electrocellpem.faraday_efficiency(arCurrentDensity)

        for e in self.electrolyserunits:
            arV_cell = self.electrocellpem.arV_cell
            rA_cell = self.electrocellpem.rA_cell

            if e.iControlLevel == 1:  # Electrolyser level
                arV_s = arV_cell * e.iN_cell * e.iN_stacks * e.iN_banks
                arV_sd = (arV_cell + e.rSummedDegradation) * e.iN_cell * e.iN_stacks * e.iN_banks
                arI_s = arCurrentDensity * rA_cell
                arH2Dot_s = arFaradayEff * rConstantPart * arI_s * e.iN_cell * e.iN_stacks * e.iN_banks
            elif e.iControlLevel == 2:  # Bank level
                arV_s = arV_cell * e.iN_cell * e.iN_stacks
                arV_sd = (arV_cell + e.rSummedDegradation) * e.iN_cell * e.iN_stacks
                arI_s = arCurrentDensity * rA_cell
                arH2Dot_s = arFaradayEff * rConstantPart * arI_s * e.iN_cell * e.iN_stacks
            else:  # Stack level
                arV_s = arV_cell * e.iN_cell
                arV_sd = (arV_cell + e.rSummedDegradation) * e.iN_cell
                arI_s = arCurrentDensity * rA_cell
                arH2Dot_s = arFaradayEff * rConstantPart * arI_s * e.iN_cell

            arP_Total_s = arI_s * arV_sd
            with np.errstate(divide='ignore', invalid='ignore'):
                arEfficiency_s = (rLHV_H2 * arH2Dot_s) / arP_Total_s
                arEfficiency_s = np.nan_to_num(arEfficiency_s, nan=0.0, posinf=0.0, neginf=0.0)

            e.arV_s = arV_s
            e.arV_sd = arV_sd
            e.arI_s = arI_s
            e.arH2Dot_s = arH2Dot_s
            e.arP_Total_s = arP_Total_s
            e.arEfficiency_s = arEfficiency_s
            e.rRatedPower_s = float(arV_s[-1] * arI_s[-1])
            e.rMinPower_s = e.rRatedPower_s * e.rTurnDownRatio
            e.rAncillaryPower_s = e.rAncillaryPowerFrac * e.rRatedPower_s

    
    # WIP - not yet implemented
    def setUpElectro1(self):
        """Initialise the list of electrolyser control units, degradation arrays, and curves."""
        # ec = electroCell(zElectroCell)  # uses ec.rT (synced later per bank)
        self.electrocellpem.build_curves()
        # units: List[ElectrolyserUnit] = [] # DECLARES EMPTY LIST TO BE FILLED, ONLY CONTAINING ELECTROLYSER UNITS
        units = []
        # base = copy.deepcopy(zElectrolyserUnit)
        base = self.electrolyserunit
        base.arDegradationTotal = np.zeros(self.electrocellpem.iNumCurrent) + base.rDegradation
        base.rSummedDegradation = 1e-30
        units.append(base)
        # Replicate to iNumUnits
        for _ in range(1, base.iNumUnits):
            e = deepcopy(base)
            e.arDegradationTotal = np.zeros(self.electrocellpem.iNumCurrent) + e.rDegradation
            e.rSummedDegradation = 1e-30
            units.append(e)
        self.electrolyserunits = units
        self.electrolyser()
    

    def map_to_db_objects(self):
        """
        Map Django DB model instances → r2h2 component attributes.

        For every M2M relation on the simulation DB object, load the first
        linked record into the matching r2h2 component instance via
        ComponentBase.from_django().
        """
        import r2h2.components as components_module

        sim_db_obj = self.simulation_name   # Django Simulation model instance

        # Map:  M2M manager name on Simulation  →  r2h2 attribute name
        components = {
            'batteries':          'battery',
            'electro_cells':      'electrocellpem',
            'electrolyser_units': 'electrolyserunit',
            'thermal_properties': 'thermalproperties',
            'time_outputs':       'timeoutputs',
            'wind_inputs':        'windinputs',
        }

        for manager_name, attr_name in components.items():
            # ── Get the M2M manager ─────────────────────────────────────────
            manager = getattr(sim_db_obj, manager_name, None)
            if manager is None:
                if self.verbose:
                    print(f"[map_to_db_objects] No M2M manager '{manager_name}' on Simulation — skipping.")
                continue

            db_obj = manager.all().first()
            if db_obj is None:
                if self.verbose:
                    print(f"[map_to_db_objects] No DB record linked via '{manager_name}' — skipping.")
                continue

            # ── Get the matching r2h2 component instance ────────────────────
            component = getattr(self, attr_name, None)
            if component is None:
                if self.verbose:
                    print(f"[map_to_db_objects] r2h2 has no attribute '{attr_name}' — skipping.")
                continue

            # ── Copy every DB field that exists in the component's defaults ─
            defaults = getattr(component, '_defaults', None) or vars(component)
            mapped, skipped = 0, 0

            for field_name in defaults:
                if hasattr(db_obj, field_name):
                    db_value = getattr(db_obj, field_name)
                    if db_value is not None:
                        setattr(component, field_name, db_value)
                        mapped += 1
                else:
                    skipped += 1

            if self.verbose:
                print(
                    f"[map_to_db_objects] {manager_name!r:24s} → "
                    f"self.{attr_name:20s} | "
                    f"mapped={mapped:3d}  skipped={skipped:3d}  "
                    f"(DB record: {db_obj})"
                )

    # ---  SIMULATION RUN FUNCTION  --- #
    def run(self,
            wind_h5_path: Optional[str] = None,
            kind: Optional[str] = None,
            use_cooling_feedback: Optional[bool] = None,
            insulated: Optional[bool] = None,
            run_id: Optional[int] = None,
            progress_callback=None,
            collect_1hz_start_date: Optional[datetime.date] = None,
            collect_1hz_end_date: Optional[datetime.date] = None,
            datum_date: Optional[datetime.date] = None):
        """Run the full multi-year simulation.

        Parameters override the values set in ``__init__`` if provided.

        Args:
            wind_h5_path:             Path to a wind HDF5 file.  If ``None`` the
                                      ``windinputs`` attribute must already hold a
                                      valid :class:`WindInputs` with ``arPowerInput``
                                      and ``arTime`` populated.
            kind:                     Technology preset (``"PEM"``).
            use_cooling_feedback:     Two-pass cooling feedback loop.
            insulated:                Insulated thermal banks.
            collect_1hz_start_date:   Optional start date for 1Hz data collection.
                                      When set along with collect_1hz_end_date,
                                      1Hz data will be stored for this period.
            collect_1hz_end_date:     Optional end date for 1Hz data collection.
            datum_date:               Reference date for mapping calendar dates to hour indices.

        Returns:
            dict with keys ``YearResults``, ``Settings``, ``ElectroCell``,
            ``Runtime_s``, ``Kind``, ``UseCoolingFeedback``, ``Insulated``, ``TimeSeriesOutput``.
        """
        _kind      = kind      if kind      is not None else self.kind
        _feedback  = use_cooling_feedback if use_cooling_feedback is not None else self.use_cooling_feedback
        _insulated = insulated if insulated is not None else self.insulated
        _run_id    = run_id

        # ── 1Hz data collection setup ────────────────────────────────────────
        _collect_1hz = collect_1hz_start_date is not None and collect_1hz_end_date is not None
        _collect_1hz_start_hour = None
        _collect_1hz_end_hour = None
        _1hz_time_series_data = {}  # Will hold accumulated 1Hz data
        _hz_variables: list = []  # empty = default channels; non-empty = user selection

        def _is_cancelled():
            """Return True if the DB run record has been marked cancelled."""
            if _run_id is None:
                return False
            try:
                from dashboard.models import SimulationRun
                return SimulationRun.objects.filter(
                    pk=_run_id, status=SimulationRun.CANCELLED
                ).exists()
            except Exception:
                return False

        # ── Optionally pull DB values into component instances ───────────────
        if self.simulation_name is not None:
            self.map_to_db_objects()
            # Override 1Hz collection settings from DB only when no explicit
            # kwargs were supplied AND the DB says collection is enabled.
            # Never re-enable collection when the DB says collect_1hz_data=False.
            sim_db = self.simulation_name
            if not _collect_1hz and getattr(sim_db, 'collect_1hz_data', False):
                collect_1hz_start_date = sim_db.collect_1hz_start_date
                collect_1hz_end_date = sim_db.collect_1hz_end_date
                _collect_1hz = collect_1hz_start_date is not None and collect_1hz_end_date is not None
            elif not getattr(sim_db, 'collect_1hz_data', True):
                # Explicitly disabled in DB — ensure collection stays off even if
                # dates were somehow passed in kwargs.
                _collect_1hz = False
            # Read user-specified variable list for 1Hz capture (may be empty list meaning defaults)
            _hz_variables = list(getattr(sim_db, 'hz_variables', None) or [])
            # Override datum_date from DB if not already set
            if datum_date is None and hasattr(self.simulation_name, 'datum_date'):
                datum_date = self.simulation_name.datum_date
            # Re-derive electrolyser topology and dynamics.
            # map_to_db_objects may have copied zero-valued topology fields
            # from the DB (e.g. iN_stacks=0 meaning "not configured in DB").
            # Patch any zero topology field with the technology preset so the
            # simulation never divides by zero in dynamicControl / electrolyser().
            el = self.electrolyserunit
            topo_preset = _preset_section(_kind, "topology")
            for attr, preset_val in topo_preset.items():
                if getattr(el, attr, 0) == 0:
                    setattr(el, attr, preset_val)
            # Re-derive iNumUnits and rDivisor (may have been zeroed by DB copy)
            el.iControlLevel = getattr(el, 'iControlLevel', 2)
            if el.iControlLevel == 1:
                el.iNumUnits = el.iNumElectro
                self.simulation.rDivisor = el.iN_banks * el.iN_stacks * el.iN_cell
            elif el.iControlLevel == 2:
                el.iNumUnits = el.iNumElectro * el.iN_banks
                self.simulation.rDivisor = el.iN_stacks * el.iN_cell
            else:
                el.iNumUnits = el.iNumElectro * el.iN_banks * el.iN_stacks
                self.simulation.rDivisor = el.iN_cell
            # Re-apply dynamics preset (rRampUp/Down, rTimeConst etc.)
            el = apply_unit_profile(_kind, el)
            self.electrolyserunit = el

        # ── Load wind data ───────────────────────────────────────────────────
        if wind_h5_path is not None:
            self.windinputs = load_wind_h5(wind_h5_path)

        wind = self.windinputs
        if not hasattr(wind, 'arPowerInput') or wind.arPowerInput is None:
            raise ValueError(
                "No wind power data found.  Supply wind_h5_path= or set "
                "self.windinputs.arPowerInput before calling run()."
            )
        wind_speed_hours = None
        try:
            ws_attr = getattr(wind, 'arWindSpeed', None)
            if ws_attr is not None:
                wind_speed_hours = np.asarray(ws_attr, dtype=float).ravel()
        except Exception:
            wind_speed_hours = None

        # ── Initialise electrolyser units and PEM cell curves ────────────────
        self.setUpElectro1()
        units    = self.electrolyserunits
        ec_curves = self.electrocellpem   # already built by setUpElectro1

        # ── Load custom engineering controller (if configured) ───────────────
        _controller_fn = None
        _curtailment_cap_provider = None
        _curtailment_state = CurtailmentState(hold_seconds=1800)
        _ctrl_end_hour_buffer_map = None
        _ctrl_buffer_alias_map: Dict[str, Tuple[str, str]] = {}
        _ctrl_obj  = getattr(self.simulation_name, 'controller', None) if self.simulation_name is not None else None
        _ctrl_file = _ctrl_obj.filename if _ctrl_obj is not None else None
        if _ctrl_file:
            try:
                from r2h2.config import get_controllers_dir
                import importlib.util
                ctrl_path = get_controllers_dir() / _ctrl_file
                spec = importlib.util.spec_from_file_location("_r2h2_user_controller", ctrl_path)
                _ctrl_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(_ctrl_mod)
                _controller_fn = getattr(_ctrl_mod, 'control', None)
                _curtailment_cap_provider = getattr(_ctrl_mod, 'get_curtailment_cap_mw', None)
                if _curtailment_cap_provider is None and hasattr(_ctrl_mod, 'curtailment_cap_mw'):
                    _curtailment_cap_provider = getattr(_ctrl_mod, 'curtailment_cap_mw')
                _ctrl_end_hour_buffer_map = getattr(
                    _ctrl_mod,
                    'end_hour_buffer_map',
                    getattr(_ctrl_mod, 'END_HOUR_BUFFER_MAP', None),
                )
                if _ctrl_end_hour_buffer_map is not None and not isinstance(_ctrl_end_hour_buffer_map, dict):
                    if self.verbose:
                        print(
                            "  [run] Ignoring end_hour_buffer_map: expected dict",
                            flush=True,
                        )
                    _ctrl_end_hour_buffer_map = None
                if _controller_fn is None:
                    raise AttributeError(f"Controller file '{_ctrl_file}' has no 'control' function.")
                try:
                    _ctrl_src = Path(ctrl_path).read_text(encoding='utf-8', errors='replace')
                    _ctrl_buffer_alias_map = _parse_controller_buffer_alias_map(_ctrl_src)
                except Exception:
                    _ctrl_buffer_alias_map = {}
                if self.verbose:
                    print(f"  [run] Using custom controller: {_ctrl_file}", flush=True)
                    if _curtailment_cap_provider is not None:
                        print("  [run] Curtailment cap provider detected", flush=True)
            except Exception as _ctrl_err:
                import warnings
                warnings.warn(
                    f"Could not load custom controller '{_ctrl_file}': {_ctrl_err}. "
                    "Falling back to built-in dynamicControl.",
                    RuntimeWarning, stacklevel=2,
                )
                _controller_fn = None
                _curtailment_cap_provider = None
                _ctrl_end_hour_buffer_map = None
                _ctrl_buffer_alias_map = {}

        # ── Bank thermal states ──────────────────────────────────────────────
        el = self.electrolyserunit
        num_banks_total = el.iN_banks * el.iNumElectro
        th_template = bank_thermal_from_kind(_kind, el, insulated=_insulated)
        th_banks = [copy.deepcopy(th_template) for _ in range(num_banks_total)]

        settings = self.simulation
        battery  = self.battery
        num_hours = wind.arPowerInput.shape[1]

        # ── Align simulation time settings to the actual wind data ───────────
        # The per-hour time axis is fully determined by the wind HDF5 file.
        # Overwrite rTotalTime / rTimeStep so the simulation is self-consistent
        # regardless of what was stored in the DB or YAML.
        T_wind = len(wind.arTime)
        if T_wind > 1:
            dt_wind = float(wind.arTime[1] - wind.arTime[0])
        else:
            dt_wind = float(settings.rTimeStep) if hasattr(settings, 'rTimeStep') else 1.0
        settings.rTotalTime = float(T_wind * dt_wind)
        settings.rTimeStep  = dt_wind

        # Cap rTransientSteps to at most 10 % of T_wind (safety guard).
        max_transient = max(1, T_wind // 10)
        if int(settings.rTransientSteps) > max_transient:
            settings.rTransientSteps = max_transient

        # Always derive number of years from the total wind data length.
        # A "year" is 8760 hours; floor to at least 1.
        implied_years = max(1, round(num_hours / 8760))
        settings.iNumYears = implied_years
        # Hours in each yearly slice of the (possibly concatenated) wind data.
        hours_per_year = num_hours // implied_years
        if self.verbose:
            print(f"  [run] iNumYears set to {implied_years} "
                  f"based on wind data ({num_hours} hours, {hours_per_year} h/yr)", flush=True)

        # ── Calculate 1Hz collection hour range ───────────────────────────────
        if _collect_1hz and collect_1hz_start_date is not None and collect_1hz_end_date is not None:
            if datum_date is None:
                # Try to get datum_date from simulation if available
                datum_date = getattr(self.simulation_name, 'datum_date', None) if self.simulation_name else None
            
            if datum_date is None:
                # Fall back to 1st Jan of current year
                datum_date = datetime.date(datetime.date.today().year, 1, 1)
            
            # Calculate hour offsets from datum_date
            start_offset = (collect_1hz_start_date - datum_date).days * 24
            end_offset = (collect_1hz_end_date - datum_date).days * 24 + 23  # inclusive, end at hour 23 of end_date
            
            # Clamp to valid range [0, num_hours)
            _collect_1hz_start_hour = max(0, start_offset)
            _collect_1hz_end_hour = min(num_hours - 1, end_offset)
            
            # Ensure start <= end
            if _collect_1hz_start_hour > _collect_1hz_end_hour:
                _collect_1hz = False
                if self.verbose:
                    print(f"  [run] 1Hz collection date range invalid (start_hour={_collect_1hz_start_hour} > end_hour={_collect_1hz_end_hour}). Skipping 1Hz collection.", flush=True)
            elif self.verbose:
                n_1hz_hours = _collect_1hz_end_hour - _collect_1hz_start_hour + 1
                print(f"  [run] Collecting 1Hz data for hours {_collect_1hz_start_hour}–{_collect_1hz_end_hour} ({n_1hz_hours} hours)", flush=True)

        zYearResults = []
        t_out_prev = None

        sim_start = time.perf_counter()

        for y in range(settings.iNumYears):
            _y_offset = y * hours_per_year
            arTotalH2 = np.zeros(hours_per_year)
            replacements_at_year_start = int(battery.iNumReplacements)
            zLogOut = {
                "arSoc":             np.zeros(hours_per_year),
                "arSocMax":          np.zeros(hours_per_year),
                "arSocMin":          np.zeros(hours_per_year),
                "arSocAv":           np.zeros(hours_per_year),
                "arRCD":             np.zeros(hours_per_year),
                "arBatteryRating":   np.zeros(hours_per_year),
                "arSpillPower":      np.zeros(hours_per_year),
                "arElecOnAv":        np.zeros(hours_per_year),
                "arEtaElPeak":       np.zeros(hours_per_year),
                "arEtaSystemPeak":   np.zeros(hours_per_year),
                "arHourlyDegradation": np.zeros((units[0].iNumUnits, hours_per_year)),
                "arWindPowerFilt":        np.zeros(hours_per_year),
                "arWindSpeed":           np.zeros(hours_per_year),
                "arAvailablePower":       np.zeros(hours_per_year),
                "arTotalElectroDemand":   np.zeros(hours_per_year),
            }

            # Lagged cooling predictor: use previous hour's cooling output as
            # the feedback estimate for the current hour.  Thermal states
            # change slowly hour-to-hour so this is accurate while eliminating
            # the expensive first-pass deepcopy (th_banks / battery / units)
            # that the two-pass approach required.  Reset each year so the
            # first hour always starts unconstrained.
            _cooling_feedback_prev: Optional[np.ndarray] = None

            for h in range(hours_per_year):
                # Check for user cancellation every hour
                if h % 10 == 0 and _is_cancelled():
                    raise InterruptedError('Simulation cancelled by user.')

                # Emit progress
                if progress_callback is not None:
                    try:
                        progress_callback(y, int(settings.iNumYears), h, hours_per_year)
                    except Exception:
                        pass

                # Track global hour across all years
                global_hour = _y_offset + h
                ctrl_input_initial_soc = float(getattr(battery, 'arInitialSoC', np.nan))
                if t_out_prev is not None:
                    ctrl_input_ai_is_on_prev = np.asarray(t_out_prev.aiIsOn[:, -1], dtype=np.int8)
                else:
                    ctrl_input_ai_is_on_prev = np.zeros(units[0].iNumUnits, dtype=np.int8)

                P_hour = wind.arPowerInput[:, global_hour]

                units, t_out, battery, th_banks = runElectroStackStep1(
                    ec_curves, th_banks, battery, P_hour,
                    units, wind.arTime, settings, global_hour, t_out_prev,
                    cooling_power_feedback=_cooling_feedback_prev if _feedback else None,
                    controller_fn=_controller_fn,
                    curtailment_state=_curtailment_state,
                    curtailment_cap_provider=_curtailment_cap_provider,
                )

                if _feedback:
                    _cooling_feedback_prev = t_out.arP_cool_elec_total.copy()

                # Update battery first so battery.* aliases (e.g. battery.arSoC)
                # are available for this hour's 1Hz collection.
                battery = runBattery1(t_out, battery, settings)

                _collected_hour_start = None
                _collected_hour_len = 0

                # ── Collect 1Hz data if this hour is in the collection range ─────
                if _collect_1hz and _collect_1hz_start_hour is not None and _collect_1hz_end_hour is not None:
                    if _collect_1hz_start_hour <= global_hour <= _collect_1hz_end_hour:
                        hz_start = int(settings.rTransientSteps)
                        hz_start = max(0, min(hz_start, len(t_out.arAvailablePower)))
                        n_hz = len(t_out.arAvailablePower) - hz_start
                        if n_hz <= 0:
                            continue

                        # Initialize 1Hz arrays on first collection
                        if not _1hz_time_series_data:
                            _1hz_time_series_data['time_indices'] = []
                        
                        # Append transient-trimmed 1Hz data for this hour.
                        # Build indices from the last appended sample so the
                        # saved time axis is always sequential (no overlaps or
                        # backward jumps at hour boundaries).
                        _collected_hour_start = len(_1hz_time_series_data['time_indices'])
                        _collected_hour_len = n_hz
                        if _1hz_time_series_data['time_indices']:
                            t_idx = int(_1hz_time_series_data['time_indices'][-1]) + 1
                        else:
                            t_idx = 0
                        _1hz_time_series_data['time_indices'].extend(
                            range(t_idx, t_idx + n_hz)
                        )

                        # Capture only user-selected 1Hz variables from t_out.
                        # Empty selection means retain no 1Hz channel data.
                        _n_full_axis = len(t_out.arAvailablePower)

                        def _resolve_hz_selected_value(selected_name):
                            mapping = _ctrl_buffer_alias_map.get(selected_name)
                            if mapping is not None:
                                src_name, attr_name = mapping
                                if src_name == 'battery':
                                    return getattr(battery, attr_name, None)
                                if src_name == 't_out':
                                    return getattr(t_out, attr_name, None)
                                return None
                            return getattr(t_out, selected_name, None)

                        for var_name in _hz_variables:
                            buf_val = _resolve_hz_selected_value(var_name)
                            if buf_val is None:
                                continue
                            buf_arr = np.asarray(buf_val)
                            if buf_arr.ndim == 0:
                                # Allow scalar aliases by broadcasting over this hour.
                                try:
                                    scalar_val = float(buf_arr)
                                except Exception:
                                    continue
                                buf_arr = np.full(n_hz, scalar_val, dtype=np.float64)
                            elif buf_arr.ndim != 1:
                                if self.verbose:
                                    print(
                                        f"  [run] Skipping {var_name}: expected a 1-D time array, got ndim={buf_arr.ndim}",
                                        flush=True,
                                    )
                                continue
                            buf_arr = buf_arr.astype(np.float64, copy=False)
                            if buf_arr.size == 0:
                                continue
                            if buf_arr.size == _n_full_axis:
                                buf_arr = buf_arr[hz_start:]
                            elif buf_arr.size == n_hz + 1:
                                # Battery arrays (e.g. arSoC) are computed from
                                # rTransientSteps-1, giving one extra leading sample.
                                buf_arr = buf_arr[1:]
                            elif buf_arr.size == 1:
                                buf_arr = np.full(n_hz, float(buf_arr[0]), dtype=np.float64)
                            elif buf_arr.size != n_hz:
                                if self.verbose:
                                    print(
                                        f"  [run] Skipping {var_name}: length {buf_arr.size} "
                                        f"does not match trimmed 1Hz axis {n_hz} "
                                        f"(or full axis {_n_full_axis})",
                                        flush=True,
                                    )
                                continue
                            if var_name not in _1hz_time_series_data:
                                _1hz_time_series_data[var_name] = []
                            _1hz_time_series_data[var_name].extend(buf_arr)
                # Expose end-of-hour battery SoC on t_out so controller-level
                # end-hour buffer mapping can reference it directly.
                t_out.arBatterySoC = float(getattr(battery, 'arInitialSoC', np.nan))

                # Optional end-of-hour snapshots from controller-defined mapping.
                # This runs after hourly post-processing so mapped values reflect
                # the final state for this hour.
                mapped_vals = _apply_end_hour_buffer_map(
                    t_out,
                    _ctrl_end_hour_buffer_map,
                    verbose=self.verbose,
                )
                if (
                    mapped_vals
                    and _collect_1hz
                    and _collected_hour_start is not None
                    and _collected_hour_len > 0
                    and 'time_indices' in _1hz_time_series_data
                ):
                    total_points = len(_1hz_time_series_data['time_indices'])
                    seg0 = _collected_hour_start
                    seg1 = _collected_hour_start + _collected_hour_len
                    for buf_name, val in mapped_vals.items():
                        if buf_name not in _hz_variables:
                            continue
                        if buf_name not in _1hz_time_series_data:
                            _1hz_time_series_data[buf_name] = [np.nan] * total_points
                        elif len(_1hz_time_series_data[buf_name]) < total_points:
                            _1hz_time_series_data[buf_name].extend(
                                [np.nan] * (total_points - len(_1hz_time_series_data[buf_name]))
                            )
                        _1hz_time_series_data[buf_name][seg0:seg1] = [float(val)] * _collected_hour_len

                zLogOut["arSoc"][h]           = battery.arInitialSoC
                zLogOut["arSocMax"][h]         = battery.rSocMax
                zLogOut["arSocMin"][h]         = battery.rSocMin
                zLogOut["arSocAv"][h]          = battery.rSocAv
                zLogOut["arRCD"][h]            = battery.rRCD
                zLogOut["arBatteryRating"][h]  = battery.rBatteryRating
                # Spill power is the mean over the transient-skipped portion
                _skip = int(settings.rTransientSteps)
                zLogOut["arSpillPower"][h]     = float(np.mean(battery.arSpillPower[_skip:])) if len(battery.arSpillPower) > _skip else 0.0
                _skip = int(settings.rTransientSteps)
                zLogOut["arElecOnAv"][h]       = float(np.nanmean(t_out.arTotalElectroOn[_skip:]))
                zLogOut["arEtaElPeak"][h]      = float(np.nanmax(t_out.arEta_el_total[_skip:]))
                zLogOut["arEtaSystemPeak"][h]  = float(np.nanmax(t_out.arEta_system_total[_skip:]))
                zLogOut["arWindPowerFilt"][h]      = float(np.nanmean(t_out.arWindPowerFilt[_skip:]))
                if wind_speed_hours is not None and global_hour < wind_speed_hours.size:
                    zLogOut["arWindSpeed"][h] = float(wind_speed_hours[global_hour])
                zLogOut["arAvailablePower"][h]     = float(np.nanmean(t_out.arAvailablePower[_skip:]))
                zLogOut["arTotalElectroDemand"][h] = float(np.nanmean(t_out.arTotalElectroDemand[_skip:]))
                for i in range(units[0].iNumUnits):
                    zLogOut["arHourlyDegradation"][i, h] = units[i].rSummedDegradation

                produced_h2   = float(np.sum(t_out.arProducedH2Dot))
                arTotalH2[h]  = (arTotalH2[h - 1] + produced_h2) if h > 0 else produced_h2
                t_out_prev    = t_out

                if self.verbose:
                    print(f"  year {y+1}/{settings.iNumYears}  hour {h+1}/{hours_per_year}  "
                          f"H2={produced_h2:.3f} g/s  SoC={battery.arInitialSoC:.3f}",
                          flush=True)

            # Add end-of-year metadata to Log
            replacements_cumulative = int(battery.iNumReplacements)
            replacements_this_year = max(0, replacements_cumulative - replacements_at_year_start)
            # Keep legacy key as yearly count so multi-year summaries can sum safely.
            zLogOut['iNumReplacements'] = replacements_this_year
            zLogOut['iNumReplacementsYear'] = replacements_this_year
            zLogOut['iNumReplacementsCumulative'] = replacements_cumulative
            zLogOut['rFinalBatteryRating'] = float(battery.rBatteryRating)

            zYearResults.append({
                "ElectrolyserUnit": copy.deepcopy(units),
                "Battery":          copy.deepcopy(battery),
                "ThermalBanks":     copy.deepcopy(th_banks),
                "TotalH2":          arTotalH2.copy(),
                "Log":              zLogOut,
            })

        runtime = time.perf_counter() - sim_start
        if self.verbose:
            print(f"Simulation complete in {runtime:.2f} s")

        # ── Convert 1Hz time series lists to numpy arrays ──────────────────────
        # Free the list immediately after conversion to avoid holding both
        # the Python list and the numpy array in memory simultaneously.
        time_series_output = None
        if _collect_1hz and _1hz_time_series_data and _hz_variables:
            def _pop_array(key, dtype):
                arr = np.array(_1hz_time_series_data.pop(key, []), dtype=dtype)
                return arr

            time_series_output = {
                'start_hour': int(_collect_1hz_start_hour) if _collect_1hz_start_hour is not None else 0,
                'end_hour': int(_collect_1hz_end_hour) if _collect_1hz_end_hour is not None else 0,
                'time_indices': _pop_array('time_indices', np.int64),
            }

            # Include only the user-selected variables that were actually collected.
            for var_name in list(_1hz_time_series_data.keys()):
                if var_name not in time_series_output and _1hz_time_series_data[var_name]:
                    time_series_output[var_name] = _pop_array(var_name, np.float64)

            # If no selected channels produced valid 1Hz arrays, do not retain a 1Hz payload.
            if len(time_series_output) <= 3:
                time_series_output = None
            if self.verbose:
                if time_series_output is None:
                    print("  [run] No selected 1Hz channels produced valid time-series output", flush=True)
                else:
                    n_points = len(time_series_output['time_indices'])
                    print(f"  [run] Collected {n_points} 1Hz data points", flush=True)

        return {
            "YearResults":          zYearResults,
            "Settings":             settings,
            "ElectroCell":          ec_curves,
            "Runtime_s":            runtime,
            "Kind":                 _kind,
            "UseCoolingFeedback":   _feedback,
            "Insulated":            _insulated,
            "TimeSeriesOutput":     time_series_output,
        }
