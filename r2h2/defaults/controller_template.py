"""
default_controller.py — R2H2 Default Engineering Controller
============================================================

This file is the built-in template controller shipped with R2H2.
Copy and rename this file to create a custom controller, then assign
it to a simulation in the Controller tab.

Function signature
------------------
The controller receives each hour:

  units    – list of ElectrolyserUnit objects (one per electrolyser stack)
  battery  – Battery object
  t_out    – TimeOutput object for this hourly step
  settings – SimulationSettings object (rTimeStep, rTransientSteps, …)

Optional curtailment lever (inside ``control`` output):

    t_out.arCurtailmentCapMW = <scalar or 1-D array>

Example 1-D profile:

    T_ctrl = len(t_out.arAvailablePower)
    t_out.arCurtailmentCapMW = np.full(T_ctrl, 60.0)
    t_out.arCurtailmentCapMW[: T_ctrl // 2] = 90.0

Accepted forms:
- scalar MW value (constant command)
- 1-D MW array of length ``T_ctrl`` (controller axis)

Core enforcement rules (applied by R2H2, not by the controller):
- cap is an absolute farm cap in MW
- changed cap must be held for at least 30 minutes
- cap ramps at 0.1 * N MW/s, where N = number of electrolysers
- ramp applies to cap trajectory only; realized wind may fall faster if raw wind drops

Note: curtailment commands returned by ``control`` are applied from the next
hour onward.

Required return value: the tuple ``(units, t_out, battery)`` with at minimum:

- ``t_out.arTotalElectroDemand``   – total electrolyser demand [W], 1-D length T_ctrl
- ``t_out.aiIsOn``                 – ON/OFF status per unit, shape (num_units, T_ctrl)
- ``t_out.arProportionPower``      – per-unit power fractions, shape (num_units, T_ctrl)

Controller window length
------------------------
Custom controllers are called with a transient-trimmed time axis only:

- ``T_ctrl = T_full - settings.rTransientSteps``

where ``T_full`` is the full per-hour internal axis (for example 3600 at 1 Hz).
You do NOT need to handle transient slots in your controller.
R2H2 pre-fills those slots from the previous hour and merges your T_ctrl outputs
back into the full arrays after your function returns.

Key controller inputs available on t_out
-----------------------------------------
- ``t_out.arAvailablePower``    – filtered wind power after ancillary load [W]
- ``t_out.aiIsOn[:, -1]``       – ON/OFF state carried over from the previous hour
- ``t_out.arTotalElectroOn``    – running count of ON units (trimmed axis)
- ``t_out.arProportionPower``   – per-unit fractions (trimmed axis)
- ``t_out.aiWarmedUp``          – warm-up mask per unit (1 = warmed up)
- ``t_out.aiNumOn``             – per-hour turn-on count per unit

Key unit attributes
-------------------
  units[i].rMinPower_s         – minimum operating power per unit [W/s]
  units[i].rRatedPower_s       – rated power per unit [W/s]
  units[i].rDeadBandRatio      – dead-band ratio (unit turns on when
                                  available_power > rMinPower_s * rDeadBandRatio)
  units[i].rSummedDegradation  – cumulative degradation (used for priority ranking)
  units[i].rTotalTurnOns       – total turn-on events (for O&M accounting)

Key battery attributes
----------------------
  battery.rControlTargetSoC    – target state-of-charge (0–1, default 0.5)
  battery.rBatteryProportionalGain
  battery.rBatteryRating       – current usable capacity [J]
  battery.arInitialSoC         – SoC at start of this hour (scalar or array)
  battery.arBatteryDemand      – battery power demand [W] — set by controller

Design implemented here
------------------------
  1. Battery SoC proportional regulator
  2. First-order exponential smoothing of available power (τ = 30 s)
  3. Electrolyser on/off dispatch:
       • turn ON  least-degraded units first when more power is available
       • turn OFF most-degraded units first when power drops
  4. Equal power sharing among ON units (proportional fractions)
"""

import numpy as np


def control(units, battery, t_out, settings):
    """Built-in dispatch controller.

    Required outputs (for downstream simulation):
    - t_out.arTotalElectroDemand: total electrolyser demand profile [W], length T_ctrl.
    - t_out.aiIsOn: ON/OFF status matrix, shape (num_units, T_ctrl).
    - t_out.arProportionPower: per-unit demand fractions, shape (num_units, T_ctrl).

    """
    # ------------------------------------------------------------------
    # Controller inputs (made explicit for readability)
    # ------------------------------------------------------------------
    # 1) Total incoming power profile [W], length T_ctrl.
    #    This axis already excludes the transient warm-up region.
    total_available_power = np.asarray(t_out.arAvailablePower, dtype=float)
    T = len(total_available_power)

    # Optional curtailment lever for next-hour command scheduling.
    # Example: 90 MW for the first half-hour, then 60 MW for the second half-hour.
    t_out.arCurtailmentCapMW = np.full(T, 60.0, dtype=float)
    t_out.arCurtailmentCapMW[: T // 2] = 90.0

    # 2) Electrolyser ON/OFF matrix [num_units, T_ctrl].
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

    # The transient slots are handled by R2H2 before/after controller call.
    # Dispatch therefore runs directly over the trimmed T_ctrl axis.
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

