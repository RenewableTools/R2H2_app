from .base import ComponentBase


class TimeOutputs(ComponentBase):
    """Container for per-timestep simulation output arrays."""

    def __init__(self, orm_object=None):
        self.arTime                  = None
        self.arWindPowerRaw          = None
        self.arWindPowerCurtailed    = None
        self.arCurtailmentRequestedCap = None
        self.arCurtailmentAppliedCap = None
        self.arWindPowerFilt         = None
        self.arAvailablePower        = None
        self.arElectroAvailablePowerA = None
        self.arElectroAvailablePower = None
        self.rPreviousValue          = 0.0
        self.arTotalElectroDemand    = None
        self.arProportionPower       = None
        self.aiIsOn                  = None
        self.aiWarmedUp              = None
        self.aiNumOn                 = None
        self.arTotalElectroOn        = None
        # Unit-level
        self.arElectroDemand             = None
        self.arI_unit                    = None
        self.arV_unit                    = None
        self.arV_unitUseful              = None
        self.arPower_unit                = None
        self.arPower_unitUseful          = None
        self.arDegradationInEfficiency   = None
        self.arV_cell                    = None
        self.arProducedH2Dot             = None
        self.arHydroEfficiency           = None
        # Global traces
        self.arP_el_total        = None
        self.arT_stack           = None
        self.arH2Dot_total       = None
        self.arV_cell_avg        = None
        self.arEta_el_total      = None
        self.arEta_system_total  = None
        # Per-bank traces
        self.arP_el_banks        = None
        self.arT_banks           = None
        # Per-stack telemetry
        self.arP_el_unit         = None
        self.arQ_gain_unit       = None
        self.arVtn_unit          = None
        self.arT_unit_bank       = None
        # Per-bank thermal diagnostics
        self.arQ_gain_banks      = None
        self.arQ_lost_banks      = None
        self.arQ_cool_banks      = None
        self.arP_cool_elec_banks = None
        self.arG_eq_banks        = None
        self.arC_th_banks        = None
        # Thermal totals
        self.arQ_gain_total      = None
        self.arQ_lost_total      = None
        self.arQ_cool_total      = None
        self.arP_cool_elec_total = None

        # Optional controller debug buffers (user-defined values captured at 1 Hz).
        # Custom controllers can populate any subset of arBuffer1..arBuffer20.
        self.arBuffer1  = None
        self.arBuffer2  = None
        self.arBuffer3  = None
        self.arBuffer4  = None
        self.arBuffer5  = None
        self.arBuffer6  = None
        self.arBuffer7  = None
        self.arBuffer8  = None
        self.arBuffer9  = None
        self.arBuffer10 = None
        self.arBuffer11 = None
        self.arBuffer12 = None
        self.arBuffer13 = None
        self.arBuffer14 = None
        self.arBuffer15 = None
        self.arBuffer16 = None
        self.arBuffer17 = None
        self.arBuffer18 = None
        self.arBuffer19 = None
        self.arBuffer20 = None

        super().__init__(orm_object=orm_object)
