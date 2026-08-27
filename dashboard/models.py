from django.db import models
import datetime
from django.core.validators import MinValueValidator, MaxValueValidator


def _controller_storage():
    """Deferred FileSystemStorage pointing at <data_root>/controllers/.
    Callable so it is evaluated at runtime, not at import / migration time."""
    from django.core.files.storage import FileSystemStorage
    from r2h2.config import get_controllers_dir
    return FileSystemStorage(location=str(get_controllers_dir()), base_url=None)


#### ---------------------- CONTROLLER MODEL ---------------------- ####

class Controller(models.Model):
    """Represents a custom engineering controller Python file stored in <data_root>/controllers/."""
    name        = models.CharField(max_length=100, default='Controller',
                                   help_text='Display name for this controller.')
    file        = models.FileField(
                      storage=_controller_storage,
                      upload_to='',
                      unique=True,
                      db_column='filename',
                      help_text='The .py controller file stored in <data_root>/controllers/. '
                                'Upload here or create/edit via the simulation editor.')
    description = models.TextField(blank=True, default='',
                                   help_text='Optional description of what this controller does.')
    author      = models.CharField(max_length=150, blank=True, default='',
                                   help_text='Name of the person who created this controller.')
    date_created = models.DateField(default=datetime.date.today,
                                    help_text='Date this controller was first created.')
    verified    = models.BooleanField(default=False,
                                      help_text='Mark True once the controller has been reviewed and validated.')
    edit_history = models.JSONField(
        default=list, blank=True,
        help_text='List of {"datetime": "ISO-8601", "author": "..."} entries, most recent last.')

    class Meta:
        ordering = ['name']

    @property
    def filename(self):
        """Return the bare filename string (e.g. 'my_ctrl.py')."""
        return self.file.name if self.file else ''

    def __str__(self):
        verified_tag = ' \u2713' if self.verified else ''
        return f"{self.name} ({self.filename}){verified_tag}"


def default_start_date():
    """Return 1st Jan of the current year."""
    return datetime.date(datetime.date.today().year, 1, 1)


#### ---------------------- SIMULATION MODEL (TOP-LEVEL) ---------------------- ####


class Simulation(models.Model):
    name = models.CharField(max_length=100, default='Simulation')
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=100, default='User')  # Placeholder for user association
    # RELATIONSHIPS TO COMPONENT MODELS (MANY-TO-MANY)
    batteries = models.ManyToManyField('Battery', blank=True)
    electro_cells = models.ManyToManyField('ElectroCellPEM', blank=True)
    electrolyser_units = models.ManyToManyField('ElectrolyserUnit', blank=True)
    thermal_properties = models.ManyToManyField('ThermalProperties', blank=True)
    time_outputs = models.ManyToManyField('TimeOutput', blank=True)
    wind_inputs = models.ManyToManyField('WindInput', through='SimulationWindInput', blank=True)
    # SIMULATION SETTINGS
    iWindType = models.PositiveIntegerField(default=0, choices=[(0, '1 sec'), (1, '1 min'), (2, '10 min'), (3, 'hourly')])
    rTotalTime = models.FloatField(default=3700.0, help_text="Total simulation time in seconds")
    rTimeStep = models.FloatField(default=1.0, help_text="Time step in seconds")
    rTransientSteps = models.PositiveIntegerField(default=101, help_text="Number of initial transient steps to discard in analysis")
    bSingleTurb = models.BooleanField(default=True, help_text="Whether to simulate a single turbine or multiple (weak grid)")
    arLateralDistances = models.JSONField(default=None, blank=True, null=True, help_text="Lateral distances of turbines in weak grid scenario (meters)")
    rDivisor = models.FloatField(default=None, blank=True, null=True, help_text="Divisor for normalizing wind power input (e.g. max power of single turbine)")
    duration_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Override simulation duration (days). Truncates wind data to this many days. Leave blank to use the full wind file.')
    datum_date = models.DateField(
        default=default_start_date,
        help_text='Start date (00:00) used as the datetime axis origin for results charts. Defaults to 1st Jan of the current year.')
    start_date = models.DateField(
        null=True, blank=True,
        help_text='Start date for the simulation when using a specific date range.')
    end_date = models.DateField(
        null=True, blank=True,
        help_text='End date for the simulation when using a specific date range.')
    controller_file = models.CharField(
        max_length=255, blank=True, default='',
        help_text='[Legacy] Filename of a custom controller. Superseded by controller FK.')
    controller  = models.ForeignKey(
        'Controller',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='simulations',
        help_text='Custom engineering controller to use for this simulation. '
                  'Leave blank to use the built-in default.')
    # 1HZ DATA COLLECTION SETTINGS
    collect_1hz_data = models.BooleanField(
        default=True,
        help_text='Enable collection of per-second (1Hz) data over a limited time period.')
    collect_1hz_start_date = models.DateField(
        null=True, blank=True,
        help_text='Start date for 1Hz data collection (inclusive). Must be before or equal to end_date. Long ranges create very large output files.')
    collect_1hz_end_date = models.DateField(
        null=True, blank=True,
        help_text='End date for 1Hz data collection (inclusive). Must be after or equal to start_date. Long ranges create very large output files.')
    hz_variables = models.JSONField(
        default=list, blank=True,
        help_text='List of t_out attribute names to capture at 1Hz. Empty list means capture all default channels. '
                  'Only arrays of length T (time) are valid.')

    def clean(self):
        """Validate date ranges for simulation and 1Hz data collection."""
        from django.core.exceptions import ValidationError

        # 1. Simulation date range
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError('Simulation start_date must be before or equal to end_date.')

        # 2. 1Hz data collection settings
        if self.collect_1hz_data:
            if not self.collect_1hz_start_date or not self.collect_1hz_end_date:
                raise ValidationError(
                    'Both start and end dates must be set when collecting 1Hz data.')
            if self.collect_1hz_start_date > self.collect_1hz_end_date:
                raise ValidationError(
                    '1Hz data collection start_date must be before or equal to end_date.')

            # 1Hz range must be within the main simulation range, if specified.
            if self.start_date and self.collect_1hz_start_date < self.start_date:
                raise ValidationError(
                    '1Hz data collection cannot start before the main simulation start date.')
            if self.end_date and self.collect_1hz_end_date > self.end_date:
                raise ValidationError(
                    '1Hz data collection cannot end after the main simulation end date.')

    def __str__(self):
        return f"ID: {self.id}, Name: {self.name}"

    def get_max_end_date(self):
        """Calculate the maximum possible end date based on linked wind data."""
        from datetime import timedelta
        if not self.datum_date:
            return None
        total_hours = sum(wi.ts_n_hours or 0 for wi in self.wind_inputs.all())
        if total_hours > 0:
            return self.datum_date + timedelta(days=int(total_hours / 24))
        return None


#### ---------------------- SIMULATION <-> WIND INPUT THROUGH TABLE ---------------------- ####

class SimulationWindInput(models.Model):
    simulation = models.ForeignKey(Simulation, on_delete=models.CASCADE, related_name='wind_input_entries')
    wind_input = models.ForeignKey('WindInput', on_delete=models.CASCADE, related_name='simulation_entries')
    sequence = models.PositiveIntegerField(default=0, help_text='Order/sequence of this wind input within the simulation.')

    class Meta:
        ordering = ['sequence']
        unique_together = [('simulation', 'wind_input')]

    def __str__(self):
        return f"Sim {self.simulation_id} – WindInput {self.wind_input_id} (seq={self.sequence})"


#### ---------------------- BATTERY MODEL ---------------------- ####

# DB TABLE DEFINITION FOR BATTERY MODEL
class Battery(models.Model):
    name = models.CharField(max_length=100, default='Battery')
    # GENERAL
    rBatteryMWh = models.FloatField(default=15, help_text="Size of the battery")
    rControlTargetSoC = models.FloatField(default=0.5, help_text="Target state of charge for the battery controller. The proportional controller drives SoC toward this value (can be above or below the current SoC).")
    rBatteryProportionalGain = models.FloatField(default=0.0, help_text="Proportional gain of controller in bringing charge back to target")
    rReplacementThreshold = models.FloatField(default=0.7, help_text="Replacement threshold fraction of initial battery capacity (0-1)")
    rTcRef = models.FloatField(default=55.0, help_text="Tc Ref (°C)")
    rSoCRef = models.FloatField(default=0.5, help_text="SoC Ref")
    arInitialSoC = models.FloatField(default=0.5, help_text="Initial SoC")
    # DEGRADATION
    rKt = models.FloatField(default=4.14e-10, help_text="Elapsed time degradation factor")
    rKs = models.FloatField(default=1.04, help_text="State of charge degradation factor")
    KTemp = models.FloatField(default=6.93e-2, help_text="Temperature degradation factor")
    rKd1 = models.FloatField(default=1.4e5, help_text="Depth of discharge degradation factor 1")
    rKd2 = models.FloatField(default=-5.01e-1, help_text="Depth of discharge degradation factor 2")
    rKd3 = models.FloatField(default=-1.23e5, help_text="Depth of discharge degradation factor 3")
    # SEI FILM
    rAlphaSei = models.FloatField(default=5.75e-2, help_text="Portion of the normalised energy capacity related to the solid electrolyte interphase (SEI) film")
    rBetaSei = models.FloatField(default=121.0, help_text="Part of the exponential component of normalised energy capture due to SEI film")
    # HIDDEN
    rFt = models.FloatField(default=0.0, help_text="Fatigue on battery due to temperature")                 # Found-by-code: should be nullable or removed
    rFc = models.FloatField(default=0.0, help_text="Fatigue on battery due to cycles")                      # Found-by-code: should be nullable or removed
    rInitialBatteryRating = models.FloatField(default=0.0, help_text="Initial rated energy capacity of the battery (J)")    # Found-by-code: should be nullable or removed
    rRCD = models.FloatField(default=1.0, help_text="Rated energy capacity (J)")
    iNumReplacements = models.PositiveIntegerField(default=0, help_text="Number of times the battery has been replaced")
    aiReplacementHour = models.JSONField(default=None, blank=True, null=True, help_text="Vector of the hours at which the battery was replaced (H)")
    # RUNTIME
    rSocAv = models.FloatField(default=0.0, help_text="Average State of Charge")
    rSocMax = models.FloatField(default=0.0, help_text="Max State of Charge")
    rSocMin = models.FloatField(default=0.0, help_text="Min State of Charge")
    rDodAv = models.FloatField(default=0.0, help_text="Average Depth of Discharge")
    arBatteryPower = models.JSONField(default=None, blank=True, null=True, help_text="Battery power flow in and out (W)")
    arSoC = models.JSONField(default=None, blank=True, null=True, help_text="State of Charge of Battery")
    arDoD = models.JSONField(default=None, blank=True, null=True, help_text="Depth of Discharge of Battery")
    # OBSOLETE?
    rBatteryRating = models.FloatField(default=0.0, help_text="Rated energy capacity (J)")
    # CONTROL
    arBatteryDemand = models.JSONField(default=None, blank=True, null=True)

    def _sync_derived_capacity_fields(self):
        """Keep Joule-based battery fields aligned with the MWh capacity."""
        try:
            capacity_j = float(self.rBatteryMWh) * 3.6e9
        except Exception:
            capacity_j = 0.0

        if capacity_j < 0.0 or not capacity_j == capacity_j:
            capacity_j = 0.0

        self.rInitialBatteryRating = capacity_j
        self.rBatteryRating = capacity_j
        self.rBatteryProportionalGain = capacity_j / 3600.0 / 10e6 if capacity_j > 0.0 else 0.0

    def clean(self):
        super().clean()
        self._sync_derived_capacity_fields()

    def save(self, *args, **kwargs):
        self._sync_derived_capacity_fields()
        return super().save(*args, **kwargs)


    def __str__(self):
        return f"ID: {self.id}, Name: {self.name} (MWh: {self.rBatteryMWh})"

    class MetaInfo:
        verbose_name = 'Battery'
        verbose_name_plural = 'Batteries'
        ui_display_fields = {
            'name':                      'Name',
            'rKt':                        'Kt (1/s)',
            'rKs':                       'Ks',
            'KTemp':                     'KTemp',
            'rAlphaSei':                 'Alpha SEI',
            'rKd1':                      'Kd1',
            'rKd2':                      'Kd2',
            'rKd3':                      'Kd3',
            'rBetaSei':                  'Beta SEI',
            'rTcRef':                    'Tc Ref (°C)',
            'rSoCRef':                   'SoC Ref',
            'arInitialSoC':              'Initial SoC',
            'rBatteryMWh':               'Capacity (MWh)',
            'rBatteryRating':            'Rating (J)',
            'rControlTargetSoC':         'Target SoC',
            'rBatteryProportionalGain':  'Proportional Gain',
            'rReplacementThreshold':     'Replacement Threshold',
            'arBatteryDemand':           'Battery Demand',
        }
        editable_groups = {
            'Battery': ['name'],
            'General': [
                'rBatteryMWh',
                'rControlTargetSoC',
                'rBatteryProportionalGain',
                'rReplacementThreshold',
                'rTcRef',
                'rSoCRef',
                'arInitialSoC',
            ],
            'Degradation': [
                'rKt',
                'rKs',
                'KTemp',
                'rKd1',
                'rKd2',
                'rKd3',
            ],
            'SEI_Film': [
                'rAlphaSei',
                'rBetaSei',
            ],
        }


#### -------------------- ELECTROCELL MODEL -------------------- ####

# DEFAULTS FOR JSON FIELDS (must be functions to avoid mutable default argument issues)
def default_faraday_temp():
    return [40.0, 60.0, 80.0]

def default_faraday_f1():
    return [150.0, 200.0, 250.0]

def default_faraday_f2():
    return [0.99, 0.985, 0.98]

# DB TABLE DEFINITION FOR ELECTROCELL MODEL
class ElectroCellPEM(models.Model):
    name = models.CharField(max_length=100, default='ElectroCellPEM')
    # UNIVERSAL CONSTANTS
    rR = models.FloatField(default=8.314, help_text="Ideal gas constant R (J/mol·K)")
    rF = models.FloatField(default=96485.0, help_text="Faraday Const F (C/mol)")
    # OUTPUTS POPULATED BY BUILD_CURVES()
    arCurrentDensity = models.JSONField(default=None, blank=True, null=True)
    arE_min = models.JSONField(default=None, blank=True, null=True)
    arR_cell = models.JSONField(default=None, blank=True, null=True)
    arV_cell = models.JSONField(default=None, blank=True, null=True)
    # GEOMETRY & GRID
    iNumCurrent = models.PositiveIntegerField(default=1000, help_text="Number of elements in the range of the current")
    rA_cell = models.FloatField(default=1000.0, help_text="Area of the cell")
    rI_rated = models.FloatField(default=3.0, help_text="The rated current density of the cell")
    # TEMPERATURE REFERENCES
    rT_0 = models.FloatField(default=20.0, help_text="The reference temperature for the electrolyser (i.e. the temperature used in design stage)")
    rT = models.FloatField(default=55.0, help_text="Initial operating temperature of the cell (°C)")
    # PEM VOLTAGE MODEL PARAMS
    rE_min0 = models.FloatField(default=1.55, help_text="Reference cell voltage")
    rR_0 = models.FloatField(default=0.345, help_text="Reference resistance area")
    rD_rt = models.FloatField(default=-0.0045 , help_text="Rate of change of resistance area with temperature")
    # STACK/CELL NOMINALS (INFORMATIVE)
    rV_cellNom = models.FloatField(default=2.1, help_text="Cell voltage at nominal temperature and current (V)")
    rV_bank = models.FloatField(default=633.5, help_text="Voltage of the bank (V)")
    rI_bank = models.FloatField(default=3000.0, help_text="Current for the bank (A/cm^2)")
    # FARADAY EFF PARAMS
    rF1 = models.FloatField(default=0.25, help_text="Parameter for the Faraday efficiency calculation")
    rF2 = models.FloatField(default=0.996, help_text="Parameter for the Faraday efficiency calculation")
    arFaradayTemp_C = models.JSONField(default=default_faraday_temp)
    arFaradayF1 = models.JSONField(default=default_faraday_f1)
    arFaradayF2 = models.JSONField(default=default_faraday_f2)
    
    def __str__(self):
        return f"ID: {self.id}, Name: {self.name}"

    class MetaInfo:
        verbose_name = 'Electro Cell (PEM)'
        verbose_name_plural = 'Electro Cells (PEM)'
        ui_display_fields = {
            'name':             'Name',
            'rA_cell':          'Cell area (cm²)',
            'rI_rated':         'Rated current (A/cm²)',
            'rT_0':             'Ref. temperature (°C)',
            'rE_min0':          'Ref. cell voltage (V)',
            'rR_0':             'Ref. resistance area (Ω·cm²)',
            'rD_rt':            'D_rt (Ω·cm²/°C)',
            'rF1':              'F1',
            'rF2':              'F2',
        }
        editable_groups = {
            'Electro Cell': ['name'],
            'Geometry & Grid': ['rA_cell', 'rI_rated'],
            'Temperature': ['rT_0'],
            'PEM Voltage': ['rE_min0', 'rR_0', 'rD_rt'],
            'Faraday Efficiency': ['rF1', 'rF2'],
        }


### -------------------- ELECTROLYSER UNIT MODEL -------------------- ####

class ElectrolyserUnit(models.Model):
    name = models.CharField(max_length=100, default='ElectrolyserUnit')
    # TOPOLOGY
    iN_stacks = models.PositiveIntegerField(default=4, help_text="Number of stacks in a bank")
    iN_banks = models.PositiveIntegerField(default=2, help_text="Number of banks in an electrolyser")
    iNumElectro = models.PositiveIntegerField(default=5, help_text="Number of electrolysers in the system")
    iN_cell = models.PositiveIntegerField(default=0)
    iControlLevel = models.PositiveIntegerField(default=2, choices=[(1, 'Electrolyser'), (2, 'Bank'), (3, 'Stack')])
    # DYNAMICS
    rTimeConst = models.FloatField(default=0, help_text="Time constant for the controller to smooth the power flow to the electrolyser")
    rDegradation = models.FloatField(default=1e-30, help_text="The change in degradation each time step (V)")
    rTurnDownRatio = models.FloatField(default=0.125, help_text="The turn down ratio of the electrolyser - the fraction of power that is the minimum operational power")
    r_s = models.FloatField(default=1.42e-10, help_text="The steady state degradation factor")
    r_f = models.FloatField(default=3.33e-7, help_text="The fatigue cycle degradation factor")
    r_o = models.FloatField(default=1.47e-4, help_text="The on-off degradation factor")
    rAncillaryPowerFrac = models.FloatField(default=0.0, help_text="The fraction of the rated power that must always be provided for the system (for ancillary services and BOP)")
    rDeadBandRatio = models.FloatField(default=2.0, help_text="Used by the controller")
    # RAMP_LIMITS
    rRampUp_W_s = models.FloatField(default=5.0e5, null=True, blank=True, help_text="Positive ramp rate limit for the electrolyser")
    rRampDown_W_s = models.FloatField(default=5.05e5, null=True, blank=True, help_text="Negative ramp rate limit for the electrolyser")
    # DERIVED
    iNumUnits = models.PositiveIntegerField(default=None, blank=True, null=True, help_text="The number of units in the system (worked out by the code)")
    rTotalTurnOns = models.FloatField(default=0.0)
    rSummedDegradation = models.FloatField(default=1e-30, help_text="The sum of the degradation on the unit. The initial value can be used to set a starting value for degradation")
    arDegradationTotal = models.JSONField(default=None, blank=True, null=True, help_text="The total degradation each time step (used in calcs)")
    # PERFORMANCE_CURVES
    arV_s = models.JSONField(default=None, blank=True, null=True)
    arV_sd = models.JSONField(default=None, blank=True, null=True)
    arI_s = models.JSONField(default=None, blank=True, null=True)
    arH2Dot_s = models.JSONField(default=None, blank=True, null=True)
    arP_Total_s = models.JSONField(default=None, blank=True, null=True)
    arEfficiency_s = models.JSONField(default=None, blank=True, null=True)
    rRatedPower_s = models.FloatField(default=0.0)
    rMinPower_s = models.FloatField(default=0.0)
    rAncillaryPower_s = models.FloatField(default=0.0)
    # DEGRADATIONS
    arDegradationSteady = models.JSONField(default=None, blank=True, null=True)
    arDegradationFatigue = models.JSONField(default=None, blank=True, null=True)
    arDegradationOnOff = models.JSONField(default=None, blank=True, null=True)
    # TOTALS
    rDegradationOnOffTotal = models.FloatField(default=0.0)
    rDegradationSteadyTotal = models.FloatField(default=0.0)
    rDegradationFatigueTotal = models.FloatField(default=0.0)

    def __str__(self):
        return f"ID: {self.id}, Name: {self.name}, Units: {self.iNumUnits}"

    class MetaInfo:
        verbose_name = 'Electrolyser Unit'
        verbose_name_plural = 'Electrolyser Units'
        ui_display_fields = {
            'name':                      'Name',
            'iN_stacks':                 'N_stacks',
            'iN_banks':                  'N_banks',
            'iNumElectro':               'N_electrolysers',
            'rTimeConst':                'Time constant (s)',
            'rTurnDownRatio':            'Turn-down ratio',
            'r_s':                       'r_s (1/s)',
            'r_f':                       'r_f',
            'r_o':                       'r_o (V)',
            'rAncillaryPowerFrac':       'Ancillary Power Frac.',
            'rRampUp_W_s':               'Ramp-up (W/s)',
            'rRampDown_W_s':             'Ramp-down (W/s)',
            'rSummedDegradation':        'Summed Degradation',
        }
        editable_groups = {
            'Electrolyser Unit': ['name'],
            'Topology': ['iN_stacks', 'iN_banks', 'iNumElectro'],
            'Dynamics': ['rTimeConst', 'rTurnDownRatio', 'r_s', 'r_f', 'r_o', 'rAncillaryPowerFrac'],
            'Ramp Limits': ['rRampUp_W_s', 'rRampDown_W_s'],
        }

### -------------------- THERMAL PROPERTIES MODEL -------------------- ####

class ThermalProperties(models.Model):
    name = models.CharField(max_length=100, default='ThermalProperties')
    rAmbientTemp = models.FloatField(default=15.0, help_text="Ambient temperature near electrolyser")
    rTauHeating = models.FloatField(default=1200.0, help_text="Time constant for heating")
    rTauCooling = models.FloatField(default=1800.0, help_text="Time constant for cooling")
    rTargetTemp = models.FloatField(default=60.0, help_text="Target operating temperature (°C)")
    rMinTemp = models.FloatField(default=50.0, help_text="Minimum operating temperature (°C)")
 
    def __str__(self):
        return f"ID: {self.id}, Name: {self.name}, Target Temp: {self.rTargetTemp}°C"

    class MetaInfo:
        verbose_name = 'Thermal Properties'
        verbose_name_plural = 'Thermal Properties'
        ui_display_fields = {
            'name':          'Name',
            'rAmbientTemp':  'Ambient Temp. (°C)',
            'rTauHeating':   'Heating Time Const. (s)',
            'rTauCooling':   'Cooling Time Const. (s)',
            'rTargetTemp':   'Target Temp. (°C)',
        }
        editable_groups = {
            'Thermal Properties': ['name'],
            'Temperature Settings': ['rAmbientTemp', 'rTauHeating', 'rTauCooling', 'rTargetTemp'],
        }

### --------------------- TIME OUTPUT MODEL -------------------- ####

class TimeOutput(models.Model):
    name = models.CharField(max_length=100, default='TimeOutput')
    arTime = models.JSONField(default=None, blank=True, null=True)
    arWindPowerFilt = models.JSONField(default=None, blank=True, null=True)
    arAvailablePower = models.JSONField(default=None, blank=True, null=True)
    arElectroAvailablePowerA = models.JSONField(default=None, blank=True, null=True)
    arElectroAvailablePower = models.JSONField(default=None, blank=True, null=True)
    rPreviousValue = models.FloatField(default=0.0)
    arTotalElectroDemand = models.JSONField(default=None, blank=True, null=True)
    arProportionPower = models.JSONField(default=None, blank=True, null=True)
    aiIsOn = models.JSONField(default=None, blank=True, null=True)
    aiWarmedUp = models.JSONField(default=None, blank=True, null=True)
    aiNumOn = models.JSONField(default=None, blank=True, null=True)
    arTotalElectroOn = models.JSONField(default=None, blank=True, null=True)
    # Unit-level outputs (2D: units x time)
    arElectroDemand = models.JSONField(default=None, blank=True, null=True)
    arI_unit = models.JSONField(default=None, blank=True, null=True)
    arV_unit = models.JSONField(default=None, blank=True, null=True)
    arV_unitUseful = models.JSONField(default=None, blank=True, null=True)
    arPower_unit = models.JSONField(default=None, blank=True, null=True)
    arPower_unitUseful = models.JSONField(default=None, blank=True, null=True)
    arDegradationInEfficiency = models.JSONField(default=None, blank=True, null=True)
    arV_cell = models.JSONField(default=None, blank=True, null=True)
    arProducedH2Dot = models.JSONField(default=None, blank=True, null=True)
    arHydroEfficiency = models.JSONField(default=None, blank=True, null=True)
    # NEW global traces
    arP_el_total = models.JSONField(default=None, blank=True, null=True)
    arT_stack = models.JSONField(default=None, blank=True, null=True)
    arH2Dot_total = models.JSONField(default=None, blank=True, null=True)
    arV_cell_avg = models.JSONField(default=None, blank=True, null=True)
    arEta_el_total = models.JSONField(default=None, blank=True, null=True)
    arEta_system_total = models.JSONField(default=None, blank=True, null=True)
    # NEW per-bank traces
    arP_el_banks = models.JSONField(default=None, blank=True, null=True)     # [num_banks_total, T]
    arT_banks = models.JSONField(default=None, blank=True, null=True)        # [num_banks_total, T]
    # *** NEW: per-stack telemetry ***
    arP_el_unit = models.JSONField(default=None, blank=True, null=True)      # [num_units, T] electrical input per stack (with degradation)
    arQ_gain_unit = models.JSONField(default=None, blank=True, null=True)    # [num_units, T] heat generated per stack = I*(V - V_TN_stack)
    arVtn_unit = models.JSONField(default=None, blank=True, null=True)       # [num_units, T] thermoneutral stack voltage
    arT_unit_bank = models.JSONField(default=None, blank=True, null=True)    # [num_units, T] bank temperature seen by each stack
    # *** NEW: per-bank thermal diagnostics ***
    arQ_gain_banks = models.JSONField(default=None, blank=True, null=True)   # [num_banks_total, T] heat input to each bank
    arQ_lost_banks = models.JSONField(default=None, blank=True, null=True)   # [num_banks_total, T] heat lost to ambient
    arQ_cool_banks = models.JSONField(default=None, blank=True, null=True)   # [num_banks_total, T] heat removed by coolant
    arP_cool_elec_banks = models.JSONField(default=None, blank=True, null=True)  # [num_banks_total, T] electrical power for cooling (COP-based)
    arG_eq_banks = models.JSONField(default =list)     # [num_banks_total, T] equivalent conductance W/K
    arC_th_banks = models.JSONField(default=None, blank=True, null=True)     # [num_banks_total, T] thermal capacitance J/K
    # *** NEW: thermal totals (all banks) ***
    arQ_gain_total = models.JSONField(default=None, blank=True, null=True)   # [T]
    arQ_lost_total = models.JSONField(default=None, blank=True, null=True)   # [T]
    arQ_cool_total = models.JSONField(default=None, blank=True, null=True)   # [T]
    arP_cool_elec_total = models.JSONField(default=None, blank=True, null=True)  # [T]

    def __str__(self):
        return f"ID: {self.id}, Name: {self.name}, Time Steps: {len(self.arTime) if self.arTime else 0}"
    
class WindInput(models.Model):
    name = models.CharField(max_length=100, default='WindInput')
    wind_file = models.FileField(
        upload_to='wind_data/',
        blank=True,
        null=True,
        help_text='HDF5 wind data file (.h5 / .hdf5 / .hdf)',
    )
    arPowerInput = models.JSONField(default=None, blank=True, null=True)
    arTime = models.JSONField(default=None, blank=True, null=True)

    # HDF5 introspection metadata (populated on upload)
    h5_datasets     = models.TextField(blank=True, default='',
                          help_text='Comma-separated list of dataset paths found in the HDF5 file.')
    ts_start        = models.FloatField(null=True, blank=True,
                          help_text='First time value in the per-hour time axis [s].')
    ts_end          = models.FloatField(null=True, blank=True,
                          help_text='Last time value in the per-hour time axis [s].')
    ts_resolution   = models.FloatField(null=True, blank=True,
                          help_text='Time step size [s].')
    ts_n_hours      = models.IntegerField(null=True, blank=True,
                          help_text='Number of hours (columns) in the wind dataset.')

    def __str__(self):
        return f"ID: {self.id}, Name: {self.name}, File: {self.wind_file or '—'}"

    class MetaInfo:
        verbose_name = "Wind Input"
        verbose_name_plural = "Wind Inputs"
        ui_display_fields = {
            'id': 'ID',
            'name': 'Name',
            'wind_file': 'Wind File',
            'ts_start': 'Start Time (s)',
            'ts_end': 'End Time (s)',
            'ts_resolution': 'Time Step (s)',
            'ts_n_hours': 'Number of Hours',
            'arPowerInput': 'Power Input Curve',
            'arTime': 'Time Axis',
            'h5_datasets': 'HDF5 Fields',
        }
        editable_groups = {
            'Wind Input': ['name'],
            'File': ['wind_file'],
            'Time Series': ['ts_n_hours', 'ts_resolution', 'ts_start', 'ts_end'],
        }

#### ---------------------- SIMULATION RUN (JOB TRACKING) ---------------------- ####

class SimulationRun(models.Model):
    PENDING   = 'pending'
    RUNNING   = 'running'
    DONE      = 'done'
    ERROR     = 'error'
    CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (PENDING,   'Pending'),
        (RUNNING,   'Running'),
        (DONE,      'Done'),
        (ERROR,     'Error'),
        (CANCELLED, 'Cancelled'),
    ]

    simulation  = models.ForeignKey(Simulation, on_delete=models.CASCADE, related_name='runs')
    status      = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    message     = models.TextField(blank=True, default='')
    description = models.CharField(max_length=200, blank=True, default='',
                                   help_text='Optional user note for this run (up to 200 chars).')
    started_at  = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    output_path = models.CharField(max_length=512, blank=True, default='',
                                   help_text='Relative path (under MEDIA_ROOT/outputs/) to the saved HDF5 results file.')
    run_start_date = models.DateField(null=True, blank=True,
                                      help_text='Effective start date used for this run (derived from simulation date range).')
    run_end_date   = models.DateField(null=True, blank=True,
                                      help_text='Effective end date used for this run (derived from simulation date range).')
    xaxis_datetime = models.BooleanField(default=True,
                                         help_text='Display results charts with a datetime x-axis (False = hours index).')
    app_version = models.CharField(max_length=20, blank=True, default='',
                                   help_text='Version of r2h2 that produced this run.')
    git_hash = models.CharField(max_length=40, blank=True, default='',
                                help_text='Git commit hash of the code that produced this run.')
    progress_checkpoints = models.JSONField(
        default=list, blank=True,
        help_text='List of {"t": <epoch_float>, "h": <sim_hours_done>} snapshots '
                  'used to compute a robust ETA that is resilient to hibernation.')
    progress_pct = models.IntegerField(
        null=True, blank=True,
        help_text='Last recorded percent complete (0–100); persisted so the UI can '
                  'show a meaningful progress bar immediately on page load/reload.')
    est_finish_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Last server-computed estimated finish time; persisted so the UI '
                  'can show an ETA immediately on page load/reload.')

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f"Run #{self.id} \u2013 {self.simulation.name} [{self.status}]"

    @property
    def duration_seconds(self):
        """Return elapsed seconds, or None if not finished."""
        if self.finished_at and self.started_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    @property
    def duration_hms(self):
        """Return elapsed time as hh:mm:ss string, or None if not finished."""
        s = self.duration_seconds
        if s is None:
            return None
        total = int(s)
        h, rem = divmod(total, 3600)
        m, sec = divmod(rem, 60)
        return f'{h:02d}:{m:02d}:{sec:02d}'