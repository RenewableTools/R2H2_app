"""
switchOffAvoidingController.py — R2H2 Engineering Controller
============================================================

This controller starts from the built-in dynamicControl logic and adds a
plant-level switch-off avoidance hold.

If the standard dispatch would turn units off because filtered available power
falls below the minimum power needed to keep them on, the controller first
tries to cover that shortfall with the battery. Battery support is limited to:

  1. the same power-rate and SoC floor protections used by dynamicControl
  2. a whole-plant hold timer of 120 seconds of active support

If the battery cannot fully cover the shortfall, the controller keeps as many
units on as the supported power can sustain and only turns off the remainder.
If the extra support requirement returns to zero, the hold timer is reset.
"""

import numpy as np


def control(units, battery, t_out, settings):
    """Built-in dispatch controller with switch-off avoidance support.

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
    initial_soc_scalar = float(np.atleast_1d(initial_soc).ravel()[-1])

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
    if initial_soc_scalar <= 0.0:
        battery.arBatteryDemand = np.clip(battery.arBatteryDemand, 0.0, per_sec_limit)

    t_out.arElectroAvailablePowerA = np.maximum(
        t_out.arAvailablePower - battery.arBatteryDemand, 0.0
    )

    # Exponential smoothing (first-order low-pass)
    tau = 30
    dt = settings.rTimeStep
    alpha = dt / (tau + dt)
    t_out.arElectroAvailablePower = np.zeros_like(t_out.arElectroAvailablePowerA)

    rMin   = units[0].rMinPower_s
    rRated = units[0].rRatedPower_s

    hold_max_s = 120.0
    hold_remaining_s = float(
        getattr(battery, "rSwitchOffAvoidingHoldRemaining_s", hold_max_s)
    )

    t_out.arSwitchOffAvoidingSupport = np.zeros(T)
    t_out.arSwitchOffAvoidingTimer = np.zeros(T)
    t_out.arSwitchOffAvoidingDeficit = np.zeros(T)

    # The transient slots are handled by R2H2 before/after controller call.
    # Dispatch therefore runs directly over the trimmed T_ctrl axis.
    for k in range(T):
        if k == 0:
            t_out.arElectroAvailablePower[k] = t_out.arElectroAvailablePowerA[k]
        else:
            t_out.arElectroAvailablePower[k] = (
                alpha * t_out.arElectroAvailablePowerA[k]
                + (1.0 - alpha) * t_out.arElectroAvailablePower[k - 1]
            )

        t_out.aiIsOn[:, k] = t_out.aiIsOn[:, k - 1]
        total_on_prev = int(t_out.arTotalElectroOn[k - 1])
        available_power = float(t_out.arElectroAvailablePower[k])
        support_required = 0.0
        support_applied = 0.0

        arMaxElectro_k = int(np.floor(available_power / rMin)) if rMin > 0.0 else total_on_prev
        arMinElectro_k = int(np.ceil(available_power / rRated)) if rRated > 0.0 else 0

        if arMinElectro_k > total_on_prev and available_power > rMin * units[0].rDeadBandRatio:
            hold_remaining_s = hold_max_s
            rank = np.argsort(degradation)
            need = arMinElectro_k - total_on_prev
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

        elif arMaxElectro_k < total_on_prev:
            support_required = max(total_on_prev * rMin - available_power, 0.0)
            if support_required <= 0.0:
                hold_remaining_s = hold_max_s
                t_out.arTotalElectroOn[k] = total_on_prev
            else:
                if initial_soc_scalar > 0.0 and hold_remaining_s > 0.0:
                    support_headroom = max(float(battery.arBatteryDemand[k]) + per_sec_limit, 0.0)
                    support_applied = min(support_required, support_headroom)

                if support_applied > 0.0:
                    battery.arBatteryDemand[k] -= support_applied
                    t_out.arElectroAvailablePowerA[k] += support_applied
                    available_power += support_applied
                    t_out.arElectroAvailablePower[k] = available_power
                    hold_remaining_s = max(hold_remaining_s - dt, 0.0)

                max_units_supported = (
                    int(np.floor(available_power / rMin)) if rMin > 0.0 else total_on_prev
                )
                target_on = max(0, min(total_on_prev, max_units_supported))

                if target_on < total_on_prev:
                    rank = np.argsort(degradation)
                    need = total_on_prev - target_on
                    for idx in rank:
                        if need <= 0:
                            break
                        if t_out.aiIsOn[idx, k] == 1:
                            t_out.aiIsOn[idx, k] = 0
                            need -= 1

                t_out.arTotalElectroOn[k] = np.sum(t_out.aiIsOn[:, k])

        else:
            hold_remaining_s = hold_max_s
            t_out.arTotalElectroOn[k] = total_on_prev

        if t_out.arTotalElectroOn[k] > 0:
            t_out.arProportionPower[:, k] = (
                t_out.aiIsOn[:, k] / t_out.arTotalElectroOn[k]
            )

        t_out.arSwitchOffAvoidingSupport[k] = support_applied
        t_out.arSwitchOffAvoidingDeficit[k] = support_required
        t_out.arSwitchOffAvoidingTimer[k] = hold_remaining_s

    t_out.rPreviousValue = float(t_out.arElectroAvailablePower[-1])
    battery.rSwitchOffAvoidingHoldRemaining_s = hold_remaining_s

    # Required controller output: total demand profile.
    t_out.arTotalElectroDemand = np.clip(
        t_out.arElectroAvailablePower,
        rMin * t_out.arTotalElectroOn,
        rRated * t_out.arTotalElectroOn,
    )
    buffer = {
    "soc": battery.arSoC,
    "switch_off_hold_timer_s": t_out.arSwitchOffAvoidingTimer,
    "switch_off_hold_support_w": t_out.arSwitchOffAvoidingSupport,
    "switch_off_hold_deficit_w": t_out.arSwitchOffAvoidingDeficit,
    }

    return units, t_out, battery