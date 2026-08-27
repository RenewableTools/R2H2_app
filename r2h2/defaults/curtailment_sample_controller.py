"""
curtailment_sample_controller.py
================================

Sample custom controller that demonstrates:
1) control(units, battery, t_out, settings) dispatch output contract
2) setting the curtailment lever directly in control(...)

Important behavior:
- The cap command is an absolute farm cap in MW.
- Core simulation enforces:
  - minimum 30-minute hold between accepted cap changes
  - cap ramp limit of 0.1 * N MW/s (N = number of electrolysers)
- The ramp applies to cap trajectory only, not directly to realized wind.
- Commands emitted in this hour are applied from the next hour.
"""

import numpy as np


def control(units, battery, t_out, settings):
    """Simple dispatch that consumes available power and shares equally.

    Required outputs:
    - t_out.arTotalElectroDemand [T]
    - t_out.aiIsOn [num_units, T]
    - t_out.arProportionPower [num_units, T]
    """
    available = np.asarray(t_out.arAvailablePower, dtype=float)
    T = len(available)
    n_units = len(units)

    r_min = float(units[0].rMinPower_s)
    r_rated = float(units[0].rRatedPower_s)

    t_out.arTotalElectroDemand = np.zeros(T, dtype=float)
    t_out.aiIsOn[:, :] = 0
    t_out.arProportionPower[:, :] = 0.0

    # Curtailment lever for next hour: set a half-hour profile over T_ctrl.
    # This repeats each hour as [90 MW for first half, 60 MW for second half].
    t_out.bCurtailmentOn = True
    half = max(T // 2, 1)
    t_out.arCurtailmentCapMW = np.full(T, 60.0, dtype=float)
    t_out.arCurtailmentCapMW[:half] = 90.0

    for k in range(T):
        p = max(float(available[k]), 0.0)

        # How many units can be ON while respecting per-unit min and rated bounds.
        if r_min > 0.0:
            n_on_max = int(np.floor(p / r_min))
        else:
            n_on_max = n_units
        if r_rated > 0.0:
            n_on_min = int(np.ceil(p / r_rated))
        else:
            n_on_min = 0

        n_on = max(n_on_min, 0)
        n_on = min(n_on, n_units)
        n_on = min(n_on, max(n_on_max, 0))

        if n_on <= 0:
            continue

        t_out.aiIsOn[:n_on, k] = 1
        t_out.arProportionPower[:n_on, k] = 1.0 / float(n_on)
        t_out.arTotalElectroDemand[k] = p

    return units, t_out, battery
