# Dynamic Controller User Guide

## Overview

R2H2 supports two controller modes:

- Built-in controller: `dynamicControl`
- Custom controller: user-provided Python function named `control`

The controller is executed once per simulation hour and generates per-second dispatch arrays for that hour.

If a custom controller is configured, R2H2 loads it at run start and calls `control` each hour. If loading, execution, or validation fails, R2H2 automatically falls back to built-in `dynamicControl`.

### Key Outputs
The controller MUST provide the outputs:
  - `t_out.arTotalElectroDemand`
  - `t_out.aiIsOn`
  - `t_out.arProportionPower`

They must have the shape 
  - `len(arTotalElectroDemand) == T_ctrl`
  - `shape(aiIsOn) == (num_units, T_ctrl)`
  - `shape(arProportionPower) == (num_units, T_ctrl)`

Where `num_units` is the number of electrolyser units in the simulation (default is 10 units) and:

- `T_full` is the full per-hour internal axis (for example 3600 at 1 Hz)
- `T_ctrl = T_full - settings.rTransientSteps`

The controller only sees `T_ctrl` samples (transient removed). R2H2 pre-fills transient slots from the previous hour and merges your controller outputs back into the full arrays.


## Custom Controller Function 

Your controller file must define:

```python
def control(units, battery, t_out, settings):
    # update units, battery, t_out
    return units, t_out, battery
```

Optional curtailment lever (set inside `control`):

```python
t_out.bCurtailmentOn = True                  # required enable flag
t_out.arCurtailmentCapMW = 50.0              # scalar MW command
t_out.arCurtailmentCapMW = np.full(T_ctrl, 50.0)  # per-second MW profile
```

To update curtailment from `control`, provide both:
- `t_out.bCurtailmentOn`
- `t_out.arCurtailmentCapMW` (or `t_out.rCurtailmentCapMW`)

Semantics:
- `bCurtailmentOn = True` with a valid cap updates the requested cap.
- `bCurtailmentOn = False` disables curtailment (wind passes through uncapped).
- Missing either field means no curtailment command update.

Curtailment enforcement is always in core simulation code:
- changed cap values are accepted no more frequently than once every 30 minutes
- cap trajectory ramps at 0.1 * N MW/s (N = number of electrolysers)
- realized wind input is `min(raw_wind, ramped_cap)` each second
- the ramp limit applies to cap trajectory only, not directly to realized wind
- commands emitted in one hour are applied from the next hour onward

Backward compatibility note:
- legacy module-level `get_curtailment_cap_mw(...)` / `curtailment_cap_mw` is still accepted
- new controllers should set `t_out.bCurtailmentOn` and cap inside `control`

### Inputs

Your function receives:

1. `units`
- List of electrolyser unit objects.
- Commonly used fields:
  - `rSummedDegradation` - This is the total degradation on the electrolyser unit
  - `rMinPower_s` - This is the minimum power that the unit can demand
  - `rRatedPower_s` - This is the maximum power that the unit can demand
  - `rTotalTurnOns` - This is how many times the unit has been turned on or off

2. `battery`
- Battery state and control parameters.
- Common fields:
  - `arInitialSoC` - This is the state of charge at the start of the time step
  - `rBatteryRating` - This is the current maximum rating of the battery
  - `rControlTargetSoC` - This is the target State of charge for the batteries inbuilt controller (defaulted to OFF so you can ignore!)
  - `rBatteryProportionalGain` - This is the proportional controller gain for the battery's in built controller (defaults to 0 so you can ignore) 

3. `t_out`
- Hourly per-second output container, pre-initialized before controller call.
- Arrays are transient-trimmed (length `T_ctrl`) in the controller interface.
- Required outputs:
  - `arTotalElectroDemand` - The total energy sent to ALL of the electrolyser units added together
  - `aiIsOn` - A set of 0 and 1 integers that set if the electrolyser units are on (1) or off (0)
  - `arProportionPower` - The proportion of arTotalElectroDemand that is sent to the relevant unit


4. `settings`
- Simulation settings for refernece.
- Common fields:
  - `rTimeStep` - The time step of the simulation (1Hz)
  - `rTransientSteps` - The transient step count used by R2H2 to build `T_ctrl`

### Return Value

Return exactly a 3-tuple:

- `units`
- `t_out`
- `battery`

## Validation and Fallback Behavior

- There is a 30 s wall-clock timeout per hourly call.
- Exception-safe execution with fallback.
- The following required outputs must exist:
  - `t_out.arTotalElectroDemand`
  - `t_out.aiIsOn`
  - `t_out.arProportionPower`
- They shoud have the right shapes. Shape checks:
  - `len(arTotalElectroDemand) == T_ctrl`
  - `shape(aiIsOn) == (num_units, T_ctrl)`
  - `shape(arProportionPower) == (num_units, T_ctrl)`
- NaN/Inf guard on required arrays.

If validation fails for any reason, R2H2 emits a warning and falls back to `dynamicControl` for that hour.

## Built-In Controller Behavior (`dynamicControl`)

The built in controller operates on a "hesitant-on, hesitant-off" basis.  It will always try to keep the same number of electrolysers on at each time step as were on in the previous time step.  If the total power drops below the minimum for the number of electrolyser units that are on (i.e. the total power is less than n_units*0.125*rated power) then the most degraded electrlyser will be switched off.  If the total power goes above the rated power of the electrolyser units that are currently on then another will be switched on.

In addition, the controller filters the power input by a time constant tau (default to 30.0). The difference between the actual power and the filtered power is then sent to the battery so that the power experienced by the electrolysers is filtered by the time constant.

Finally, the controller also aims to keep the state of charge of the battery close to the desired state of charge (set to 0.5), with a deadband of 0.1 and a proportional gain applied if the state of charge moves outwith this deadband.

The power to the electrlysers is divided equally between the electrolyser units.

## Post-Controller Physical Guards (Applied To All Controllers)

After controller return, R2H2 applies additional execution guards:

- Non-binary `aiIsOn` entries are treated as invalid and replaced with `0`.
  - Per-second invalid-count is recorded in `t_out.aiAssignmentError`.
  - Cleaned ON matrix is stored in `t_out.aiIsOn_clean`.
- Total ON count is recomputed from cleaned `aiIsOn`.
- Total demand is clipped to:
  - lower bound: `min_power * arTotalElectroOn`
  - upper bound: `rated_power * arTotalElectroOn`
- If too many units are ON to satisfy per-unit minimum power, units are switched OFF.
  - Remaining demand is reallocated across ON units with bounded allocation.
- Per-unit ramp-rate and min/max bounds are enforced in the per-second loop.

This means controller outputs are treated as requested dispatch and may be adjusted to satisfy physical feasibility.

## Minimal Custom Controller Example

This is a very basic controller that you could use as a starting point for designing a controller.

```python
import numpy as np


def control(units, battery, t_out, settings):
    T_ctrl = len(t_out.arAvailablePower)
    n_units = len(units)

    # Required output 1: total fleet demand [W], length T_ctrl
    t_out.arTotalElectroDemand = np.asarray(t_out.arAvailablePower, dtype=float).copy()

    # Required output 2: ON/OFF matrix, shape (n_units, T_ctrl)
    t_out.aiIsOn[:, :] = 1

    # Required output 3: per-unit proportions, shape (n_units, T_ctrl)
    t_out.arProportionPower[:, :] = 0.0
    on_count = np.sum(t_out.aiIsOn, axis=0).astype(float)
    on_mask = on_count > 0
    if np.any(on_mask):
        t_out.arProportionPower[:, on_mask] = (
            t_out.aiIsOn[:, on_mask] / on_count[on_mask]
        )

    return units, t_out, battery
```

Note: the controller does not need to handle transient indices. Work directly on the full `T_ctrl` arrays that are provided.


## 1 Hz Collection Model (Current Behavior)

When 1 Hz collection is enabled, R2H2 collects only variables listed in `Simulation.hz_variables`.

Important current behavior:

- If `hz_variables` is empty, no 1 Hz channel datasets are produced.
- `time_indices` is always sequential across collected hours.
- **Only successfully resolved and shape-compatible variables are stored.**
- If no selected channels produce data, `TimeSeriesOutput` is omitted.


### Accepted Shapes for Selected Variables

Per selected variable during hourly collection:

- Scalar: broadcast to that hour's trimmed 1 Hz length.
- 1-D array of full hourly axis length `T`: transient-trimmed.
- 1-D array of trimmed length `n_hz`: used directly.
- 1-D array of length `n_hz + 1`: first sample dropped (battery-style arrays).
- 1-D array of length `1`: broadcast.

Skipped:

- Non-1-D arrays.
- 1-D arrays with unsupported lengths.

## `buffer = {...}` Alias Map for 1 Hz Selection

R2H2 parses controller source for a top-level variable named `buffer`. As an example:

```python
buffer = {
    "soc": battery.arSoC,
    "total_on": t_out.arTotalElectroOn,
}
```

If you add alias keys there, those keys can be selected in `hz_variables` and resolved at runtime.

Notes:

- Parser accepts assignments to `buffer` in `Assign` or `AnnAssign` forms.
- *Alias values must be attribute references on `battery` or `t_out`.* - To be clear `"my_var":my_var` won't work, `"my_var":t_out.my_var` will (assuming the variable has the right size!)


## Recommended Workflow

1. Start from minimal required outputs.
2. Validate shape and finite numeric values every hour.
3. Run short windows first.
4. Add optional `buffer` aliases and/or `end_hour_buffer_map` only as needed.
5. Select only the 1 Hz variables you need to limit output size.
6. Scale duration after behavior is stable.

## Troubleshooting

Symptom: custom controller appears ignored.

Common causes:

- Missing `control` function.
- Wrong return tuple format.
- Shape mismatch in required arrays.
- NaN/Inf in required arrays.
- Runtime exception in controller.
- 30 s timeout in controller call.

Curtailment note:
- Invalid/non-finite curtailment commands are ignored.
- Missing commands keep the last accepted cap.

Checks:

1. `control(units, battery, t_out, settings)` exists.
2. Return is exactly `(units, t_out, battery)`.
3. Required arrays have correct dimensions.
4. Required arrays are finite.
5. If collecting 1 Hz channels, ensure selected variable names exist and are 1-D compatible.
