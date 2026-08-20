# Django specific libraries
from django.shortcuts import render, redirect
from django.utils.safestring import mark_safe
from django.apps import apps
from django.views.decorators.http import require_POST
import json

# Django REST framework
# ...

# Std libraries
import re
from django.utils.html import escape

# Django models
from django.db import models as _db_models
from .models import *
from .models import SimulationRun

# Local src libraries
# ...



def landing(request):
    """Landing page: overview of app with workflow guide and quick stats."""
    from .models import Simulation, SimulationRun, WindInput
    context = {
        'sim_count': Simulation.objects.count(),
        'run_count': SimulationRun.objects.count(),
        'wind_count': WindInput.objects.exclude(wind_file='').count(),
        'recent_runs': SimulationRun.objects.select_related('simulation').order_by('-started_at')[:5],
    }
    return render(request, 'dashboard/landing.html', context)


def app_settings(request):
    """GET: render the settings page."""
    from r2h2.config import (
        get_config_path, load_config, get_controllers_dir, get_wind_data_dir,
    )
    from importlib.metadata import version as _pkg_version
    try:
        app_version = _pkg_version('r2h2')
    except Exception:
        app_version = 'dev'
    cfg = load_config() or {}
    data_root    = cfg.get('paths', {}).get('data_root', '')
    wind_data_dir = cfg.get('paths', {}).get('wind_data_dir', '')
    if not wind_data_dir:
        try:
            wind_data_dir = str(get_wind_data_dir())
        except Exception:
            wind_data_dir = ''
    try:
        controllers_dir = str(get_controllers_dir())
    except Exception:
        controllers_dir = ''
    return render(request, 'dashboard/settings.html', {
        'app_version':    app_version,
        'config_path':    str(get_config_path()),
        'data_root':      data_root,
        'wind_data_dir':  wind_data_dir,
        'controllers_dir': controllers_dir,
    })


@require_POST
def settings_save(request):
    """POST: persist a single settings key (data_root or wind_data_dir)."""
    from django.http import JsonResponse
    import json as _json
    from r2h2.config import update_data_root, update_wind_data_dir, invalidate_controllers_dir_cache
    try:
        body = _json.loads(request.body)
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON.'}, status=400)
    key   = body.get('key', '')
    value = body.get('value', '').strip()
    if not value:
        return JsonResponse({'ok': False, 'error': 'Value cannot be empty.'}, status=400)
    if key == 'data_root':
        try:
            update_data_root(value)
            invalidate_controllers_dir_cache()
            return JsonResponse({'ok': True, 'note': 'Restart R2H2 for the change to take full effect.'})
        except Exception as exc:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=500)
    elif key == 'wind_data_dir':
        try:
            update_wind_data_dir(value)
            return JsonResponse({'ok': True})
        except Exception as exc:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=500)
    return JsonResponse({'ok': False, 'error': f'Unknown key: {key}'}, status=400)


def help_page(request):
    """Help & guide page."""
    import sys
    import django
    from importlib.metadata import version as _pkg_version
    try:
        app_version = _pkg_version('r2h2')
    except Exception:
        app_version = 'dev'
    context = {
        'version':        app_version,
        'django_version': django.get_version(),
        'python_version': sys.version.split()[0],
    }
    return render(request, 'dashboard/help.html', context)


def _infer_simulation_end_date(sim):
    """Best-effort simulation end date inferred from current simulation settings."""
    import datetime as _dt

    start = sim.datum_date or _dt.date.today()

    candidates = []
    if sim.end_date:
        candidates.append(sim.end_date)
    if sim.duration_days:
        candidates.append(start + _dt.timedelta(days=int(sim.duration_days)))

    wind_hours = sum((wi.ts_n_hours or 0) for wi in sim.wind_inputs.all())
    if wind_hours > 0:
        candidates.append(start + _dt.timedelta(days=int(wind_hours / 24)))

    return min(candidates) if candidates else None


def _default_1hz_range(sim):
    """Return preset 1Hz range: simulation start to first 3 months or sim end."""
    import datetime as _dt

    start = sim.datum_date or _dt.date.today()
    first_three_months_end = start + _dt.timedelta(days=90)
    sim_end = _infer_simulation_end_date(sim)
    end = min(first_three_months_end, sim_end) if sim_end else first_three_months_end
    if end < start:
        end = start
    return start, end


def _ensure_1hz_preset(sim, *, persist=False):
    """Ensure preset 1Hz settings are present.

    Preset behavior:
    - Enabled by default
    - Range defaults to first 3 months from datum_date (or simulation end if shorter)
    """
    # Respect explicit user-off choice — never re-enable if user turned it off.
    if not sim.collect_1hz_data:
        return False

    needs_dates = (sim.collect_1hz_start_date is None or sim.collect_1hz_end_date is None)
    needs_enable = not sim.collect_1hz_data
    if not needs_dates and not needs_enable:
        return False

    start, end = _default_1hz_range(sim)
    sim.collect_1hz_data = True
    if needs_dates:
        sim.collect_1hz_start_date = start
        sim.collect_1hz_end_date = end

    if persist:
        update_fields = ['collect_1hz_data']
        if needs_dates:
            update_fields.extend(['collect_1hz_start_date', 'collect_1hz_end_date'])
        sim.save(update_fields=update_fields)

    return True


@require_POST
def git_pull(request):
    """POST: check for a newer release on GitHub and upgrade via pip if found.

    Works whether the app was installed via pip or cloned from GitHub.
    Falls back to `git pull` if a .git directory is present (dev installs).
    """
    from django.http import JsonResponse
    import subprocess
    import sys
    from pathlib import Path
    import importlib.metadata

    repo_root = Path(__file__).resolve().parent.parent

    # ── Dev install: fall back to git pull if .git is present ──────────────
    if (repo_root / '.git').is_dir():
        try:
            result = subprocess.run(
                ['git', 'pull'],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = (result.stdout + result.stderr).strip()
            changed = 'Already up to date.' not in output
            return JsonResponse({'ok': True, 'output': output, 'changed': changed})
        except Exception as exc:
            return JsonResponse({'ok': False, 'output': str(exc), 'changed': False}, status=500)

    # ── Pip install: check GitHub releases API then upgrade ─────────────────
    import urllib.request
    import json as _json

    GITHUB_API = 'https://api.github.com/repos/RenewableTools/R2H2_app/releases/latest'

    try:
        current_version = importlib.metadata.version('r2h2')
    except importlib.metadata.PackageNotFoundError:
        current_version = 'unknown'

    # 1. Fetch latest release tag from GitHub
    try:
        req = urllib.request.Request(GITHUB_API, headers={'Accept': 'application/vnd.github+json',
                                                           'User-Agent': 'R2H2-app'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = _json.loads(resp.read())
        latest_tag = release.get('tag_name', '').lstrip('v')
        release_url = release.get('html_url', '')
    except Exception as exc:
        return JsonResponse({'ok': False,
                             'output': f'Could not reach GitHub: {exc}',
                             'changed': False}, status=500)

    # Normalise versions for comparison
    def _ver_tuple(v):
        try:
            return tuple(int(x) for x in v.split('.'))
        except ValueError:
            return (0,)

    if _ver_tuple(latest_tag) <= _ver_tuple(current_version):
        return JsonResponse({
            'ok': True,
            'output': f'Already up to date (v{current_version}).',
            'changed': False,
        })

    # 2. Upgrade — detect pipx vs plain pip
    import shutil
    is_pipx = 'pipx/venvs' in sys.executable.replace('\\', '/')

    if is_pipx:
        pipx_bin = shutil.which('pipx')
        if not pipx_bin:
            return JsonResponse({'ok': False,
                                 'output': 'pipx not found on PATH.',
                                 'changed': False}, status=500)
        upgrade_cmd = [pipx_bin, 'upgrade', 'r2h2']
    else:
        pip_source = f'https://github.com/RenewableTools/R2H2_app/archive/refs/tags/{release["tag_name"]}.zip'
        upgrade_cmd = [sys.executable, '-m', 'pip', 'install', '--upgrade', pip_source]

    try:
        result = subprocess.run(
            upgrade_cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = (result.stdout + result.stderr).strip()
        ok = result.returncode == 0
        # On Windows, pip may fail with PermissionError because the running
        # r2h2.exe is locked.  Detect this and give a clear instruction.
        import sys as _sys
        if not ok and _sys.platform == 'win32' and (
            'permissionerror' in output.lower()
            or 'access is denied' in output.lower()
            or '.exe' in output.lower() and 'error' in output.lower()
        ):
            summary = (
                f'Upgrade to v{latest_tag} downloaded but could not replace the '
                f'running executable — Windows locks files that are in use.\n\n'
                f'To complete the upgrade:\n'
                f'  1. Close R2H2 (stop the server).\n'
                f'  2. Run: pip install --upgrade r2h2\n'
                f'  3. Restart R2H2.\n\n'
                f'Technical detail:\n{output}'
            )
            return JsonResponse({'ok': False, 'output': summary, 'changed': False,
                                 'windows_locked': True})
        summary = (f'Updated v{current_version} → v{latest_tag}\n\n{output}'
                   if ok else output)
        return JsonResponse({'ok': ok, 'output': summary, 'changed': ok})
    except Exception as exc:
        return JsonResponse({'ok': False, 'output': str(exc), 'changed': False}, status=500)


@require_POST
def restart_app(request):
    """POST: restart the R2H2 process by re-exec'ing the current Python interpreter."""
    import os
    import sys
    import threading

    def _do_restart():
        import time
        time.sleep(0.4)          # allow the JSON response to flush first
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_do_restart, daemon=True).start()
    from django.http import JsonResponse
    return JsonResponse({'ok': True})


def create_simulation(request):
    """POST: create a new Simulation with name, description and optional M2M components."""
    from django.http import JsonResponse
    from django.views.decorators.http import require_POST as _rp
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    name        = request.POST.get('name', '').strip() or 'Simulation'
    description = request.POST.get('description', '').strip()

    sim = Simulation.objects.create(name=name, description=description)
    _ensure_1hz_preset(sim, persist=True)

    # M2M: each field sent as multiple values, e.g. batteries=1&batteries=3
    m2m_map = {
        'batteries':          (sim.batteries,          Battery),
        'electro_cells':      (sim.electro_cells,      ElectroCellPEM),
        'electrolyser_units': (sim.electrolyser_units, ElectrolyserUnit),
        'thermal_properties': (sim.thermal_properties, ThermalProperties),
        'wind_inputs':        (sim.wind_inputs,        WindInput),
    }
    for field_name, (manager, model_cls) in m2m_map.items():
        ids = request.POST.getlist(field_name)
        if ids:
            objs = model_cls.objects.filter(pk__in=ids)
            manager.set(objs)

    return JsonResponse({'id': sim.id, 'name': sim.name})


def update_simulation(request, sim_id):
    """POST: update name, description and M2M components of an existing Simulation."""
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    sim = get_object_or_404(Simulation, pk=sim_id)
    name        = request.POST.get('name', '').strip()
    description = request.POST.get('description', '').strip()
    if name:
        sim.name = name
    sim.description = description
    sim.save(update_fields=['name', 'description'])

    m2m_map = {
        'batteries':          (sim.batteries,          Battery),
        'electro_cells':      (sim.electro_cells,      ElectroCellPEM),
        'electrolyser_units': (sim.electrolyser_units, ElectrolyserUnit),
        'thermal_properties': (sim.thermal_properties, ThermalProperties),
        'wind_inputs':        (sim.wind_inputs,        WindInput),
    }
    for field_name, (manager, model_cls) in m2m_map.items():
        ids = request.POST.getlist(field_name)
        objs = model_cls.objects.filter(pk__in=ids) if ids else model_cls.objects.none()
        manager.set(objs)

    return JsonResponse({'id': sim.id, 'name': sim.name})


@require_POST
def create_default_model(request):
    """POST: create a new default simulation model.

    For each component type (Battery, ElectroCellPEM, ElectrolyserUnit,
    ThermalProperties) the view looks for an existing DB record whose
    field values exactly match the current defaults.  If one is found it
    is reused; otherwise a brand-new record is created (with an
    auto-incrementing suffix so names never clash).

    A new Simulation is always created (with a unique name derived from
    "Default Model") and the resolved components are linked to it.

    Returns JSON {id: <sim_pk>} on success so the client can redirect.
    """
    from django.http import JsonResponse

    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    # ── Import defaults from the management command ──────────────────────
    from dashboard.management.commands.create_main_model import (
        _BATTERY_DEFAULTS,
        _ELECTROCELL_PEM_DEFAULTS,
        _THERMAL_DEFAULTS,
        _build_electrolyser_defaults,
        _build_simulation_defaults,
    )

    kind = 'PEM'
    el_defaults   = _build_electrolyser_defaults(kind)
    sim_def_extra = _build_simulation_defaults(kind, el_defaults)

    _SENTINEL = object()

    # Fields that are part of the component "identity" (skip name / pk /
    # array fields that don't compare cleanly with ==).
    def _match(model, defaults):
        """Return the first existing record whose non-name fields all match
        *defaults*, or None if no match exists."""
        skip = {'name', 'id'}
        for obj in model.objects.all():
            match = True
            for field, val in defaults.items():
                if field in skip:
                    continue
                db_val = getattr(obj, field, _SENTINEL)
                # Normalise None / empty list comparisons
                if db_val != val:
                    match = False
                    break
            if match:
                return obj
        return None

    def _unique_name(model, base):
        """Return *base* if unused, else *base* (2), (3) … until unique."""
        if not model.objects.filter(name=base).exists():
            return base
        n = 2
        while model.objects.filter(name=f'{base} ({n})').exists():
            n += 1
        return f'{base} ({n})'

    # ── Resolve / create each component ──────────────────────────────────
    bat_match = _match(Battery, _BATTERY_DEFAULTS)
    if bat_match:
        bat = bat_match
    else:
        bat = Battery.objects.create(
            **{**_BATTERY_DEFAULTS,
               'name': _unique_name(Battery, _BATTERY_DEFAULTS['name'])})

    ec_match = _match(ElectroCellPEM, _ELECTROCELL_PEM_DEFAULTS)
    if ec_match:
        ec = ec_match
    else:
        ec = ElectroCellPEM.objects.create(
            **{**_ELECTROCELL_PEM_DEFAULTS,
               'name': _unique_name(ElectroCellPEM, _ELECTROCELL_PEM_DEFAULTS['name'])})

    el_match = _match(ElectrolyserUnit, el_defaults)
    if el_match:
        el = el_match
    else:
        el = ElectrolyserUnit.objects.create(
            **{**el_defaults,
               'name': _unique_name(ElectrolyserUnit, el_defaults['name'])})

    th_match = _match(ThermalProperties, _THERMAL_DEFAULTS)
    if th_match:
        th = th_match
    else:
        th = ThermalProperties.objects.create(
            **{**_THERMAL_DEFAULTS,
               'name': _unique_name(ThermalProperties, _THERMAL_DEFAULTS['name'])})

    # ── Always create a new Simulation ───────────────────────────────────
    # Only pass fields that actually exist on the Simulation model
    _sim_model_fields = {f.name for f in Simulation._meta.get_fields() if hasattr(f, 'column')}
    sim_fields = {k: v for k, v in sim_def_extra.items()
                  if k not in ('name', 'description') and k in _sim_model_fields}
    sim_name = _unique_name(Simulation, 'Default Model')
    sim = Simulation.objects.create(
        name=sim_name,
        description=f'Default model created from built-in component defaults ({kind}).',
        **sim_fields,
    )
    _ensure_1hz_preset(sim, persist=True)
    sim.batteries.add(bat)
    sim.electro_cells.add(ec)
    sim.electrolyser_units.add(el)
    sim.thermal_properties.add(th)

    return JsonResponse({'id': sim.pk, 'name': sim.name,
                         'reused': {
                             'battery':      bat_match is not None,
                             'electro_cell': ec_match  is not None,
                             'electrolyser': el_match  is not None,
                             'thermal':      th_match  is not None,
                         }})


def delete_simulation(request, sim_id):
    """POST: delete a Simulation model and its associated runs."""
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404

    sim = get_object_or_404(Simulation, pk=sim_id)
    active = sim.runs.filter(status__in=('pending', 'running')).exists()
    if active:
        return JsonResponse({
            'error': 'Cannot delete a model while a run is pending or running. Cancel active runs first.'
        }, status=400)

    sim.delete()
    return JsonResponse({'deleted': sim_id})


def simulations(request):
    """Hierarchical view of Simulation models with M2M component relations."""
    try:
        _ensure_default_controller()
    except Exception:
        pass  # never break the page over a housekeeping task

    sims = Simulation.objects.prefetch_related(
        'batteries',
        'electro_cells',
        'electrolyser_units',
        'thermal_properties',
        'wind_inputs',
    ).order_by('-id')

    sim_data = []
    for sim in sims:
        sim_data.append({
            'obj': sim,
            'components': [
                {
                    'label': 'Battery',
                    'icon': 'battery_charging_full',
                    'table': 'Battery',
                    'items': list(sim.batteries.all()),
                },
                {
                    'label': 'ElectroCellPEM',
                    'icon': 'developer_board',
                    'table': 'ElectroCellPEM',
                    'items': list(sim.electro_cells.all()),
                },
                {
                    'label': 'ElectrolyserUnit',
                    'icon': 'water_do',
                    'table': 'ElectrolyserUnit',
                    'items': list(sim.electrolyser_units.all()),
                },
                {
                    'label': 'ThermalProperties',
                    'icon': 'thermostat',
                    'table': 'ThermalProperties',
                    'items': list(sim.thermal_properties.all()),
                },
                {
                    'label': 'WindInput',
                    'icon': 'air',
                    'table': 'WindInput',
                    'items': list(sim.wind_inputs.all()),
                },
            ],
        })

    nsm_sections = [
        {'field': 'batteries',          'label': 'Battery',           'icon': 'battery_charging_full', 'table': 'Battery',           'items': list(Battery.objects.order_by('name'))},
        {'field': 'electro_cells',      'label': 'ElectroCellPEM',    'icon': 'developer_board',       'table': 'ElectroCellPEM',    'items': list(ElectroCellPEM.objects.order_by('name'))},
        {'field': 'electrolyser_units', 'label': 'ElectrolyserUnit',  'icon': 'water_do',              'table': 'ElectrolyserUnit',  'items': list(ElectrolyserUnit.objects.order_by('name'))},
        {'field': 'thermal_properties', 'label': 'ThermalProperties', 'icon': 'thermostat',            'table': 'ThermalProperties', 'items': list(ThermalProperties.objects.order_by('name'))},
        {'field': 'wind_inputs',        'label': 'WindInput',         'icon': 'air',                   'table': 'WindInput',         'items': list(WindInput.objects.order_by('name'))},
    ]
    return render(request, 'dashboard/simulations.html', {
        'sim_data': sim_data,
        'nsm_sections': nsm_sections,
        'create_url': '/simulations/create/',
    })


def _model_to_sections(obj):
    """Convert a model instance into ordered display sections.
    Returns a list of {title, icon, fields: [{name, value}]}.
    Skips JSON array fields (shown separately) and private fields.
    Uses MetaInfo.ui_display_fields for human-readable labels when available.
    """
    ui_map = getattr(getattr(obj.__class__, 'MetaInfo', None), 'ui_display_fields', {})
    # Build a lookup of all concrete fields by name
    all_fields = {f.name: f for f in obj._meta.get_fields() if hasattr(f, 'column')}
    scalar_fields, array_fields = [], []

    # Iterate in ui_display_fields order when available, then append remaining fields
    ordered_names = list(ui_map.keys()) if ui_map else []
    remaining = [n for n in all_fields if n not in ui_map and n != 'id']
    for name in ordered_names + remaining:
        if name == 'id' or name not in all_fields:
            continue
        label = ui_map.get(name, name)
        value = getattr(obj, name, None)
        if isinstance(value, list):
            array_fields.append({'name': label, 'value': value})
        else:
            scalar_fields.append({'name': label, 'value': value})
    return scalar_fields, array_fields


def simulation_detail(request, sim_id):
    """Detail view for a single Simulation showing all component settings."""
    from django.shortcuts import get_object_or_404
    sim = get_object_or_404(
        Simulation.objects.prefetch_related(
            'batteries', 'electro_cells', 'electrolyser_units',
            'thermal_properties', 'time_outputs', 'wind_inputs',
        ),
        pk=sim_id
    )
    _ensure_1hz_preset(sim, persist=True)

    def component_detail(obj):
        scalar, arrays = _model_to_sections(obj)
        scalar_rows = [scalar[i:i+2] for i in range(0, len(scalar), 2)]

        meta = getattr(obj.__class__, 'MetaInfo', None)
        editable_groups = getattr(meta, 'editable_groups', {})
        ui_map = getattr(meta, 'ui_display_fields', {})
        all_fields_meta = {f.name: f for f in obj._meta.get_fields() if hasattr(f, 'column')}

        grouped_in_eg = set()
        for fnames in editable_groups.values():
            grouped_in_eg.update(fnames)

        def field_badge(name):
            label = ui_map.get(name, name)
            value = getattr(obj, name, None)
            field_obj = all_fields_meta.get(name)
            help_text = str(getattr(field_obj, 'help_text', '') or '')
            is_array = isinstance(value, list)
            if is_array:
                display = f'array [{len(value)}]' if value else 'empty'
            elif value is None:
                display = '—'
            else:
                display = str(value)
            return {'name': name, 'label': label, 'display': display,
                    'is_array': is_array, 'help_text': help_text}

        field_groups = [
            {'group': gname, 'fields': [field_badge(n) for n in fnames
                                        if n in all_fields_meta]}
            for gname, fnames in editable_groups.items()
        ]
        hidden_fields = [
            field_badge(name) for name in ui_map
            if name not in grouped_in_eg and name in all_fields_meta
        ]

        return {'obj': obj, 'scalar': scalar, 'scalar_rows': scalar_rows, 'arrays': arrays,
                'field_groups': field_groups, 'hidden_fields': hidden_fields}

    from datetime import date as _today
    _first_wind_year = sim.datum_date.year if sim.datum_date else _today.today().year

    groups = [
        {'label': 'Battery',           'icon': 'battery_charging_full', 'items': [component_detail(o) for o in sim.batteries.all()]},
        {'label': 'ElectroCellPEM',    'icon': 'developer_board',       'items': [component_detail(o) for o in sim.electro_cells.all()]},
        {'label': 'ElectrolyserUnit',  'icon': 'water_do',               'items': [component_detail(o) for o in sim.electrolyser_units.all()]},
        {'label': 'ThermalProperties', 'icon': 'thermostat',             'items': [component_detail(o) for o in sim.thermal_properties.all()]},
        {'label': 'WindInput',         'icon': 'air',                    'items': [
            {**component_detail(entry.wind_input), 'wind_year': _first_wind_year + idx}
            for idx, entry in enumerate(
                sim.wind_input_entries.select_related('wind_input').order_by('sequence')
            )
        ]},
    ]

    linked_ids = {
        'Battery':           set(sim.batteries.values_list('id', flat=True)),
        'ElectroCellPEM':    set(sim.electro_cells.values_list('id', flat=True)),
        'ElectrolyserUnit':  set(sim.electrolyser_units.values_list('id', flat=True)),
        'ThermalProperties': set(sim.thermal_properties.values_list('id', flat=True)),
        'WindInput':         set(sim.wind_inputs.values_list('id', flat=True)),
    }

    groups_with_items = [
        {
            **g,
            'available_json': json.dumps([
                {'id': o.pk, 'label': str(o)}
                for o in _GROUP_M2M[g['label']][0].objects.exclude(
                    pk__in=linked_ids[g['label']]
                ).order_by('id')
            ]),
        }
        for g in groups if g['items']
    ]
    groups_empty = [
        {
            **g,
            'available': [
                {'id': o.pk, 'label': str(o)}
                for o in _GROUP_M2M[g['label']][0].objects.exclude(
                    pk__in=linked_ids[g['label']]
                ).order_by('id')
            ],
            'available_json': json.dumps([
                {'id': o.pk, 'label': str(o)}
                for o in _GROUP_M2M[g['label']][0].objects.exclude(
                    pk__in=linked_ids[g['label']]
                ).order_by('id')
            ]),
        }
        for g in groups if not g['items']
    ]

    wind_type_label = dict(Simulation._meta.get_field('iWindType').choices).get(sim.iWindType, sim.iWindType)

    from datetime import date as _today_date, timedelta as _timedelta
    datum_display = sim.datum_date.strftime('%d %b %Y') if sim.datum_date else None
    _end_date = sim.end_date


    # Derived dates from total linked wind hours (for "All available wind data" display)
    _wind_total_hours = sum(
        wi.ts_n_hours or 0
        for wi in sim.wind_inputs.all()
    )
    if sim.datum_date and _wind_total_hours > 0:
        new_end_date = sim.datum_date + _timedelta(days=int(_wind_total_hours / 24))
        # Only auto-update end_date when in full-duration mode (no specific range set)
        if not sim.duration_days and sim.end_date != new_end_date:
            sim.end_date = new_end_date
            sim.save(update_fields=['end_date'])

    _derived_start = sim.datum_date.isoformat() if sim.datum_date else ''
    _derived_end = (
        (sim.datum_date + _timedelta(days=int(_wind_total_hours / 24))).isoformat()
        if (sim.datum_date and _wind_total_hours > 0) else ''
    )

    sim_settings = [
        {'name': 'Duration',             'value': sim.duration_days,        'unit': 'days', 'editable': 'duration_days'},
        {'name': 'Date range',           'editable': 'date_range',
         'start_date':         sim.start_date.isoformat() if sim.start_date else (sim.datum_date.isoformat() if sim.datum_date else ''),
         'end_date':           sim.end_date.isoformat() if sim.end_date else (_end_date.isoformat() if _end_date else ''),
         'derived_start_date': _derived_start,
         'derived_end_date':   _derived_end,
         'mode':               'range' if sim.duration_days else 'all'},
        {'name': 'Time step',            'value': sim.rTimeStep,            'unit': 's'},
    ]
    sim_hidden_settings = [
        {'name': 'Total time',           'value': sim.rTotalTime,           'unit': 's'},
        {'name': 'Transient steps',      'value': sim.rTransientSteps,      'unit': ''},
        {'name': 'Single turbine',       'value': sim.bSingleTurb,          'unit': ''},
        {'name': 'Lateral distances',    'value': sim.arLateralDistances,   'unit': 'm'},
        {'name': 'Power divisor',        'value': sim.rDivisor,             'unit': 'W'},
    ]
    # Group into rows of 3 for 6-column layout
    sim_settings_pairs = [sim_settings[i:i+2] for i in range(0, len(sim_settings), 2)]

    latest_run = sim.runs.first()   # newest first via Meta ordering
    sim_runs   = list(sim.runs.all())
    for run in sim_runs:
        run.has_1hz_data = _get_run_1hz_info(run)['has_1hz_data']

    nsm_sections = [
        {'field': 'batteries',          'label': 'Battery',           'icon': 'battery_charging_full', 'table': 'Battery',           'items': list(Battery.objects.order_by('name'))},
        {'field': 'electro_cells',      'label': 'ElectroCellPEM',    'icon': 'developer_board',       'table': 'ElectroCellPEM',    'items': list(ElectroCellPEM.objects.order_by('name'))},
        {'field': 'electrolyser_units', 'label': 'ElectrolyserUnit',  'icon': 'water_do',              'table': 'ElectrolyserUnit',  'items': list(ElectrolyserUnit.objects.order_by('name'))},
        {'field': 'thermal_properties', 'label': 'ThermalProperties', 'icon': 'thermostat',            'table': 'ThermalProperties', 'items': list(ThermalProperties.objects.order_by('name'))},
        {'field': 'wind_inputs',        'label': 'WindInput',         'icon': 'air',                   'table': 'WindInput',         'items': list(WindInput.objects.order_by('name'))},
    ]
    current_ids = {
        'batteries':          list(sim.batteries.values_list('id', flat=True)),
        'electro_cells':      list(sim.electro_cells.values_list('id', flat=True)),
        'electrolyser_units': list(sim.electrolyser_units.values_list('id', flat=True)),
        'thermal_properties': list(sim.thermal_properties.values_list('id', flat=True)),
        'wind_inputs':        list(sim.wind_inputs.values_list('id', flat=True)),
    }
    current_ids_json = json.dumps(current_ids)

    # Engineering controllers
    from .models import Controller
    import json as _json
    controller_files = _list_controller_files()
    controller_objects = list(Controller.objects.order_by('name').values('id', 'name', 'file', 'author', 'verified'))
    controller_files_json = _json.dumps(controller_files)

    _has_wind_linked  = sim.wind_inputs.exists()
    _any_wind_exist   = WindInput.objects.exists()

    return render(request, 'dashboard/simulation_detail.html', {
        'sim': sim,
        'sim_settings': sim_settings,
        'sim_settings_pairs': sim_settings_pairs,
        'sim_hidden_settings': sim_hidden_settings,
        'controller_files': controller_files,
        'controller_files_json': controller_files_json,
        'controller_objects': controller_objects,
        'groups': groups_with_items,
        'groups_empty': groups_empty,
        'first_wind_year': _first_wind_year,
        'latest_run': latest_run,
        'sim_runs': sim_runs,
        'nsm_sections': nsm_sections,
        'current_ids_json': current_ids_json,
        'update_url': f'/simulations/{sim_id}/update/',
        'date_range_save_url': f'/simulations/{sim_id}/date-range/',
        'has_wind_linked': _has_wind_linked,
        'any_wind_exist':  _any_wind_exist,
    })


# Map from group label → (model_class, m2m manager name on Simulation)
_GROUP_M2M = {
    'Battery':           (Battery,           'batteries'),
    'ElectroCellPEM':    (ElectroCellPEM,    'electro_cells'),
    'ElectrolyserUnit':  (ElectrolyserUnit,  'electrolyser_units'),
    'ThermalProperties': (ThermalProperties, 'thermal_properties'),
    'TimeOutput':        (TimeOutput,        'time_outputs'),
    'WindInput':         (WindInput,         'wind_inputs'),
}


def link_components(request, sim_id):
    """POST: add selected component IDs to a simulation M2M relation."""
    from django.shortcuts import get_object_or_404, redirect
    if request.method == 'POST':
        sim = get_object_or_404(Simulation, pk=sim_id)
        label = request.POST.get('group_label', '')
        ids = request.POST.getlist('component_ids')
        if label in _GROUP_M2M and ids:
            model_class, manager_name = _GROUP_M2M[label]
            objs = list(model_class.objects.filter(pk__in=ids))
            if label == 'WindInput':
                # Sort new objects alphabetically by string representation
                objs_sorted = sorted(objs, key=lambda o: str(o).lower())
                # Find next sequence number after any existing entries
                existing_max = sim.wind_input_entries.aggregate(
                    m=_db_models.Max('sequence')
                )['m']
                next_seq = (existing_max + 1) if existing_max is not None else 1
                for obj in objs_sorted:
                    SimulationWindInput.objects.get_or_create(
                        simulation=sim,
                        wind_input=obj,
                        defaults={'sequence': next_seq},
                    )
                    next_seq += 1
            else:
                getattr(sim, manager_name).add(*objs)
    return redirect('dashboard-simulation-detail', sim_id=sim_id)


def unlink_component(request, sim_id):
    """POST: remove a single component from a simulation M2M relation."""
    from django.shortcuts import get_object_or_404
    from django.http import JsonResponse
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    sim = get_object_or_404(Simulation, pk=sim_id)
    label = request.POST.get('group_label', '')
    obj_id = request.POST.get('obj_id', '')
    if label not in _GROUP_M2M or not obj_id:
        return JsonResponse({'error': 'Invalid parameters'}, status=400)
    model_class, manager_name = _GROUP_M2M[label]
    try:
        obj = model_class.objects.get(pk=obj_id)
        getattr(sim, manager_name).remove(obj)
    except model_class.DoesNotExist:
        return JsonResponse({'error': 'Object not found'}, status=404)
    # Re-sequence WindInput through-table entries to close any gaps
    if label == 'WindInput':
        for new_seq, entry in enumerate(
            sim.wind_input_entries.order_by('sequence'), start=1
        ):
            if entry.sequence != new_seq:
                entry.sequence = new_seq
                entry.save(update_fields=['sequence'])
    return JsonResponse({'unlinked': int(obj_id), 'label': label})


def reorder_wind_inputs(request, sim_id):
    """POST: update sequence numbers for WindInput through-table entries.
    Expects JSON body: {"order": [wi_id, wi_id, ...]}
    """
    from django.shortcuts import get_object_or_404
    from django.http import JsonResponse
    import json as _json
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    sim = get_object_or_404(Simulation, pk=sim_id)
    try:
        data = _json.loads(request.body)
        order = [int(x) for x in data.get('order', [])]
    except (ValueError, TypeError, _json.JSONDecodeError):
        return JsonResponse({'error': 'Invalid payload'}, status=400)
    entries = {e.wind_input_id: e for e in sim.wind_input_entries.all()}
    for seq, wi_id in enumerate(order, start=1):
        entry = entries.get(wi_id)
        if entry and entry.sequence != seq:
            entry.sequence = seq
            entry.save(update_fields=['sequence'])
    return JsonResponse({'reordered': order})


def update_wind_year(request, sim_id):
    """POST: update the year field on a SimulationWindInput through-table entry.
    Expects form fields: wind_input_id, year
    """
    from django.shortcuts import get_object_or_404
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    sim = get_object_or_404(Simulation, pk=sim_id)
    try:
        wi_id = int(request.POST.get('wind_input_id', ''))
        year  = int(request.POST.get('year', ''))
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid payload'}, status=400)
    if year < 1900 or year > 2300:
        return JsonResponse({'error': 'Year must be between 1900 and 2300'}, status=400)
    try:
        entry = sim.wind_input_entries.get(wind_input_id=wi_id)
    except SimulationWindInput.DoesNotExist:
        return JsonResponse({'error': 'Entry not found'}, status=404)
    entry.year = year
    entry.save(update_fields=['year'])
    return JsonResponse({'updated': wi_id, 'year': year})


def update_first_wind_year(request, sim_id):
    """POST: update datum_date year, keeping existing month/day (or Jan 1 if not set).
    Expects form field: year
    """
    from django.shortcuts import get_object_or_404
    import datetime as _dt
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    sim = get_object_or_404(Simulation, pk=sim_id)
    try:
        year = int(request.POST.get('year', ''))
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid year'}, status=400)
    if year < 1900 or year > 2300:
        return JsonResponse({'error': 'Year must be between 1900 and 2300'}, status=400)
    if sim.datum_date:
        try:
            new_date = sim.datum_date.replace(year=year)
        except ValueError:  # e.g. Feb 29 in non-leap year
            new_date = _dt.date(year, sim.datum_date.month, 1)
    else:
        new_date = _dt.date(year, 1, 1)
    sim.datum_date = new_date
    _wind_total_hours = sum(
        wi.ts_n_hours or 0
        for wi in sim.wind_inputs.all()
    )
    new_end = (
        new_date + _dt.timedelta(days=int(_wind_total_hours / 24))
        if _wind_total_hours > 0 else None
    )
    derived_end = new_end.isoformat() if new_end else ''
    # Always update end_date to match new datum; only update start_date/duration
    # if we are in "full duration" mode (duration_days is None / not overridden).
    save_fields = ['datum_date', 'end_date']
    sim.end_date = new_end
    if not sim.duration_days:  # full-duration mode — keep start pinned to datum
        sim.start_date = new_date
        save_fields.append('start_date')
    sim.save(update_fields=save_fields)
    return JsonResponse({
        'year': year,
        'datum_date': new_date.isoformat(),
        'derived_start': new_date.isoformat(),
        'derived_end': derived_end,
    })


def _resolve_wind_h5_path(sim):
    """Return the absolute Path to the HDF5 file from the first linked WindInput
    that has a wind_file set.  Raises ValueError if none is found."""
    from django.conf import settings as _settings
    for wi in sim.wind_inputs.exclude(wind_file='').exclude(wind_file__isnull=True):
        abs_path = _Path(_settings.MEDIA_ROOT) / wi.wind_file.name
        if abs_path.exists():
            return abs_path
    raise ValueError(
        f"Simulation '{sim.name}' has no linked WindInput with a valid wind file. "
        "Upload an HDF5 file on the Wind Data page and link it via a WindInput component."
    )


def _resolve_wind_h5_paths(sim):
    """Return ordered list of (wind_input, abs_path) for all linked WindInputs
    that have a valid wind_file, ordered by sequence.
    Raises ValueError if none found."""
    from django.conf import settings as _settings
    entries = (
        sim.wind_input_entries
        .select_related('wind_input')
        .exclude(wind_input__wind_file='')
        .exclude(wind_input__wind_file__isnull=True)
        .order_by('sequence')
    )
    paths = []
    for entry in entries:
        abs_path = _Path(_settings.MEDIA_ROOT) / entry.wind_input.wind_file.name
        if abs_path.exists():
            paths.append(abs_path)
    if not paths:
        raise ValueError(
            f"Simulation '{sim.name}' has no linked WindInput with a valid wind file. "
            "Upload an HDF5 file on the Wind Data page and link it via a WindInput component."
        )
    return paths


def _load_concatenated_wind(paths):
    """Load and concatenate wind data from multiple HDF5 paths in order.
    arPowerInput is concatenated along the hours axis (axis 1).
    arTime is kept as the single-hour time axis from the first file.
    Returns a WindInputs instance.
    """
    from r2h2.r2h2 import load_wind_h5
    from r2h2.components.WindInputs import WindInputs
    import numpy as np
    if len(paths) == 1:
        return load_wind_h5(str(paths[0]))
    segments = [load_wind_h5(str(p)) for p in paths]
    combined = WindInputs()
    combined.arPowerInput = np.concatenate(
        [s.arPowerInput for s in segments], axis=1
    )
    combined.arWindSpeed = np.concatenate(
        [np.asarray(getattr(s, 'arWindSpeed', np.array([], dtype=float)), dtype=float).ravel()
         for s in segments],
        axis=0,
    )
    # arTime is the within-hour time axis — identical across files; keep first
    combined.arTime = segments[0].arTime
    return combined


def _save_run_outputs(run, results: dict) -> str:
    """Serialise simulation results to an HDF5 file under MEDIA_ROOT/outputs/.

    Returns the relative path (from MEDIA_ROOT) of the saved file, e.g.
    ``outputs/run_42_Simulation-0_20260505-143012.h5``.

    Structure written to HDF5
    ─────────────────────────
    /meta/
        sim_name        str
        run_id          int
        kind            str
        runtime_s       float
        use_cooling_feedback  bool
        insulated       bool
    /inputs/            (snapshot of all component and simulation settings at run time)
        simulation/     (scalar attrs from Simulation model)
        battery_<N>/    (one sub-group per linked Battery)
        electrocellpem_<N>/
        electrolyserunit_<N>/
        thermalproperties_<N>/
        windinput_<N>/
    /year_<N>/          (one group per simulated year)
        battery/
            arSoc, arSocMax, arSocMin, arSocAv, arRCD, arBatteryRating  (1-D float64)
        electrolyser/
            arElecOnAv          (1-D float64)
            arHourlyDegradation (2-D float64, shape [n_units, n_hours])
        h2/
            arTotalH2           (1-D float64, cumulative H₂ [g])
    """
    import h5py
    import numpy as np
    import re
    from django.conf import settings as dj_settings
    from pathlib import Path
    from django.utils import timezone as tz

    media_root = Path(dj_settings.MEDIA_ROOT)
    out_dir = media_root / 'outputs'
    out_dir.mkdir(parents=True, exist_ok=True)

    sim_name_slug = re.sub(r'[^\w\-]', '_', run.simulation.name)[:40]
    ts = tz.localtime(run.started_at).strftime('%Y%m%d-%H%M%S') if run.started_at else 'unknown'
    filename = f'run_{run.pk}_{sim_name_slug}_{ts}.h5'
    abs_path = out_dir / filename

    year_results = results.get('YearResults', [])

    def _write_obj_attrs(grp, obj):
        """Write all concrete scalar fields of a Django model instance as HDF5 attrs."""
        for field in obj._meta.get_fields():
            if not hasattr(field, 'column'):
                continue
            name = field.name
            val = getattr(obj, name, None)
            if val is None:
                grp.attrs[name] = 'None'
            elif isinstance(val, bool):
                grp.attrs[name] = int(val)
            elif isinstance(val, (int, float, str)):
                grp.attrs[name] = val
            elif hasattr(val, 'isoformat'):      # date / datetime
                grp.attrs[name] = val.isoformat()
            elif isinstance(val, list):
                try:
                    arr = np.asarray(val)
                    if arr.dtype.kind in ('i', 'u', 'f'):
                        grp.create_dataset(name, data=arr.astype(np.float64),
                                           compression='gzip', compression_opts=4)
                    else:
                        grp.attrs[name] = str(val)
                except Exception:
                    grp.attrs[name] = str(val)
            else:
                grp.attrs[name] = str(val)

    sim_obj = run.simulation

    with h5py.File(abs_path, 'w') as f:
        # ── /meta ────────────────────────────────────────────────────────────
        meta = f.create_group('meta')
        meta.attrs['sim_name']             = sim_obj.name
        meta.attrs['run_id']               = run.pk
        meta.attrs['kind']                 = str(results.get('Kind', ''))
        meta.attrs['runtime_s']            = float(results.get('Runtime_s', 0.0))
        meta.attrs['use_cooling_feedback'] = bool(results.get('UseCoolingFeedback', False))
        meta.attrs['insulated']            = bool(results.get('Insulated', False))
        meta.attrs['app_version']          = run.app_version or ''
        meta.attrs['git_hash']             = run.git_hash or ''
        meta.attrs['run_start_date']       = run.run_start_date.isoformat() if run.run_start_date else ''
        meta.attrs['run_end_date']         = run.run_end_date.isoformat()   if run.run_end_date   else ''

        # ── /inputs ──────────────────────────────────────────────────────────
        inp = f.create_group('inputs')

        sim_grp = inp.create_group('simulation')
        _write_obj_attrs(sim_grp, sim_obj)

        for i, obj in enumerate(sim_obj.batteries.all()):
            _write_obj_attrs(inp.create_group(f'battery_{i}'), obj)
        for i, obj in enumerate(sim_obj.electro_cells.all()):
            _write_obj_attrs(inp.create_group(f'electrocellpem_{i}'), obj)
        for i, obj in enumerate(sim_obj.electrolyser_units.all()):
            _write_obj_attrs(inp.create_group(f'electrolyserunit_{i}'), obj)
        for i, obj in enumerate(sim_obj.thermal_properties.all()):
            _write_obj_attrs(inp.create_group(f'thermalproperties_{i}'), obj)
        for i, obj in enumerate(sim_obj.wind_inputs.all()):
            _write_obj_attrs(inp.create_group(f'windinput_{i}'), obj)

        # ── /year_N ──────────────────────────────────────────────────────────
        for yr_idx, yr in enumerate(year_results):
            grp = f.create_group(f'year_{yr_idx}')
            log = yr.get('Log', {})

            # Battery time-series
            bat = grp.create_group('battery')
            # Store scalar metadata as attributes
            if 'iNumReplacements' in log:
                bat.attrs['iNumReplacements'] = int(log['iNumReplacements'])
            if 'iNumReplacementsYear' in log:
                bat.attrs['iNumReplacementsYear'] = int(log['iNumReplacementsYear'])
            if 'iNumReplacementsCumulative' in log:
                bat.attrs['iNumReplacementsCumulative'] = int(log['iNumReplacementsCumulative'])
            if 'rFinalBatteryRating' in log:
                bat.attrs['rFinalBatteryRating'] = float(log['rFinalBatteryRating'])
            
            for key in ('arSoc', 'arSocMax', 'arSocMin', 'arSocAv',
                        'arRCD', 'arBatteryRating', 'arSpillPower'):
                arr = log.get(key)
                if arr is not None:
                    bat.create_dataset(key, data=np.asarray(arr, dtype=np.float64),
                                       compression='gzip', compression_opts=4)

            # Electrolyser time-series
            elec = grp.create_group('electrolyser')
            eu_list = yr.get('ElectrolyserUnit', [])
            i_num_units = eu_list[0].iNumUnits if eu_list else 0
            elec.attrs['iNumUnits'] = int(i_num_units)
            for key in ('arElecOnAv', 'arEtaElPeak', 'arEtaSystemPeak'):
                arr = log.get(key)
                if arr is not None:
                    elec.create_dataset(key, data=np.asarray(arr, dtype=np.float64),
                                        compression='gzip', compression_opts=4)
            deg = log.get('arHourlyDegradation')
            if deg is not None:
                # Store as 2-D [n_units, n_hours]; squeeze to 1-D if single unit
                elec.create_dataset('arHourlyDegradation',
                                    data=np.asarray(deg, dtype=np.float64),
                                    compression='gzip', compression_opts=4)

            # Power time-series
            pwr = grp.create_group('power')
            for key in ('arWindPowerFilt', 'arWindSpeed', 'arAvailablePower', 'arTotalElectroDemand'):
                arr = log.get(key)
                if arr is not None:
                    pwr.create_dataset(key, data=np.asarray(arr, dtype=np.float64),
                                       compression='gzip', compression_opts=4)

            # H2 production
            h2 = grp.create_group('h2')
            arr = yr.get('TotalH2')
            if arr is not None:
                h2.create_dataset('arTotalH2', data=np.asarray(arr, dtype=np.float64),
                                  compression='gzip', compression_opts=4)

        # ── /time_series_1hz (1Hz per-second data if collected) ────────────────
        ts_output = results.get('TimeSeriesOutput')
        if ts_output is not None:
            ts_grp = f.create_group('time_series_1hz')
            ts_grp.attrs['start_hour'] = int(ts_output.get('start_hour', 0))
            ts_grp.attrs['end_hour'] = int(ts_output.get('end_hour', 0))

            # Write all datasets that were collected in-memory.
            # Metadata keys are stored as attrs above.
            for key, arr in ts_output.items():
                if key in ('start_hour', 'end_hour') or arr is None:
                    continue
                ts_grp.create_dataset(
                    key,
                    data=np.asarray(arr),
                    compression='gzip',
                    compression_opts=4,
                )

    # Return path relative to MEDIA_ROOT
    return str(abs_path.relative_to(media_root))


def _get_app_version():
    """Return the installed r2h2 package version string, or 'dev'."""
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version('r2h2')
    except Exception:
        return 'dev'


def _get_git_hash():
    """Return the short git commit hash of HEAD, or empty string if unavailable."""
    import subprocess
    from pathlib import Path
    repo_root = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ['git', '-C', str(repo_root), 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ''


def _run_simulation_thread(run_id):
    """Background worker: update SimulationRun status while running."""
    from django.utils import timezone
    try:
        run = SimulationRun.objects.select_related('simulation').get(pk=run_id)
        run.status = SimulationRun.RUNNING
        run.started_at = timezone.now()
        run.app_version = _get_app_version()
        run.git_hash = _get_git_hash()
        run.save(update_fields=['status', 'started_at', 'app_version', 'git_hash'])

        import numpy as np
        import datetime as _dt
        from r2h2.r2h2 import R2H2
        wind_paths = _resolve_wind_h5_paths(run.simulation)
        sim_engine = R2H2(run.simulation)
        sim_engine.windinputs = _load_concatenated_wind(wind_paths)

        sim_obj = run.simulation
        _ensure_1hz_preset(sim_obj, persist=True)
        wi = sim_engine.windinputs
        effective_start = None
        effective_end   = None

        def _slice_wind_speed_hours(start_hour: int, end_hour: int) -> None:
            """Slice hourly wind-speed series to match arPowerInput hour window."""
            ws = getattr(wi, 'arWindSpeed', None)
            if ws is None:
                return
            ws_arr = np.asarray(ws, dtype=float).ravel()
            if ws_arr.size == 0:
                return
            sh = max(0, int(start_hour))
            eh = max(sh, int(end_hour))
            eh = min(eh, ws_arr.size)
            setattr(wi, 'arWindSpeed', ws_arr[sh:eh])

        # If a specific date range is set, slice wind data to [start_date, end_date).
        # For integer-based wind files (no real timestamps), time origin is
        # 00:00 on 1-Jan of the datum_date year.
        if sim_obj.start_date and sim_obj.end_date and sim_obj.duration_days:
            datum = sim_obj.datum_date or _dt.date(sim_obj.start_date.year, 1, 1)
            start_hour = int((sim_obj.start_date - datum).days * 24)
            end_hour   = int((sim_obj.end_date - datum).days * 24) + 24  # end_date inclusive: include all 24h
            start_hour = max(0, start_hour)
            if wi is not None and hasattr(wi, 'arPowerInput') and wi.arPowerInput is not None:
                n_hours = wi.arPowerInput.shape[1]
                end_hour = min(end_hour, n_hours)
                if start_hour < end_hour:
                    wi.arPowerInput = wi.arPowerInput[:, start_hour:end_hour]
                    _slice_wind_speed_hours(start_hour, end_hour)
            effective_start = sim_obj.start_date
            effective_end   = sim_obj.end_date
        elif sim_obj.duration_days:
            # duration_days only (no explicit date range): truncate concatenated
            # wind data from the beginning, regardless of number of source files.
            max_hours = sim_obj.duration_days * 24
            if wi is not None and hasattr(wi, 'arPowerInput') and wi.arPowerInput is not None:
                n_hours = wi.arPowerInput.shape[1]
                if max_hours < n_hours:
                    wi.arPowerInput = wi.arPowerInput[:, :max_hours]
                    _slice_wind_speed_hours(0, max_hours)
            datum = sim_obj.datum_date or _dt.date(_dt.date.today().year, 1, 1)
            effective_start = datum
            effective_end   = datum + _dt.timedelta(days=sim_obj.duration_days)

        # Persist effective date range on the run record
        run.run_start_date = effective_start
        run.run_end_date   = effective_end
        run.save(update_fields=['run_start_date', 'run_end_date'])

        _PROGRESS_INTERVAL_SECONDS = 10.0  # write a progress update every 10 s of wall time
        _progress_start = timezone.now()
        _last_progress_msg = None
        _progress_write_count = 0  # counts DB writes made so far
        _checkpoints = []  # [{"t": epoch_float, "h": sim_hours_done}, ...]

        def _on_progress(year, total_years, hour, total_hours):
            nonlocal _last_progress_msg, _progress_write_count

            total_steps = total_years * total_hours
            done_steps = year * total_hours + hour
            is_final_progress_tick = bool(total_steps) and done_steps + 1 >= total_steps

            now = timezone.now()
            elapsed = (now - _progress_start).total_seconds()

            # Write whenever elapsed crosses the next 10 s boundary
            next_threshold = (_progress_write_count + 1) * _PROGRESS_INTERVAL_SECONDS
            if elapsed < next_threshold and not is_final_progress_tick:
                return

            pct = int(done_steps / total_steps * 100) if total_steps else 0
            year_str = f'Year {year+1}/{total_years} — ' if total_years > 1 else ''
            if total_years > 1:
                hour_str = f'hour {done_steps+1}/{total_steps}'
            else:
                hour_str = f'hour {hour+1}/{total_hours}'
            msg = (f'{year_str}{hour_str}<br>{pct}\u00a0% @{elapsed:.1f}')

            # Record checkpoint: wall epoch + sim-hours done (used by poll view for robust ETA)
            import time as _wall_time
            _checkpoints.append({'t': _wall_time.time(), 'h': int(done_steps)})

            # Compute ETA now so it can be persisted to DB for page-reload use
            _eta_iso = _compute_eta_iso(_checkpoints, total_steps) if len(_checkpoints) >= 2 else None
            _update = {'message': msg, 'progress_checkpoints': _checkpoints, 'progress_pct': pct}
            if _eta_iso:
                from datetime import datetime as _dt2, timezone as _tz2
                _update['est_finish_at'] = _dt2.strptime(_eta_iso, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=_tz2.utc)
            if msg != _last_progress_msg or _eta_iso:
                SimulationRun.objects.filter(pk=run.pk).update(**_update)
                _last_progress_msg = msg
                _progress_write_count += 1

        # Prepare 1Hz collection parameters if enabled.
        # When the wind file is sliced to a simulation date range, the run's
        # time origin shifts to the first hour of that slice. Use that same
        # origin for 1Hz date offsets so the collection window stays aligned
        # with the hourly simulation timeline.
        run_datum_date = effective_start or sim_obj.datum_date
        collect_1hz_kwargs = {}
        if sim_obj.collect_1hz_data and sim_obj.collect_1hz_start_date and sim_obj.collect_1hz_end_date:
            collect_1hz_kwargs = {
                'collect_1hz_start_date': sim_obj.collect_1hz_start_date,
                'collect_1hz_end_date': sim_obj.collect_1hz_end_date,
                'datum_date': run_datum_date,
            }

        results = sim_engine.run(run_id=run.pk, progress_callback=_on_progress, **collect_1hz_kwargs)

        # Only mark DONE if user hasn't cancelled in the meantime
        run.refresh_from_db(fields=['status'])
        if run.status == SimulationRun.CANCELLED:
            return

        # Persist outputs to HDF5
        try:
            rel_path = _save_run_outputs(run, results)
            run.output_path = rel_path
        except Exception as save_exc:
            run.output_path = ''
            import logging
            logging.getLogger(__name__).warning(
                'Could not save run #%s outputs: %s', run.pk, save_exc)

        run.status  = SimulationRun.DONE
        desc = run.description.strip()
        if desc:
            suffix = desc if len(desc) <= 80 else desc[:77] + '…'
            run.message = f'Completed. {suffix}'
        else:
            run.message = f'Simulation \u201c{run.simulation.name}\u201d completed successfully.'
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'message', 'finished_at', 'output_path'])
        # Flush the WAL file now that the simulation is done.  We disabled
        # automatic checkpointing to avoid mid-run exclusive locks (which
        # cause Windows UI freezes); manually checkpoint here instead.
        try:
            from django.db import connection as _db_conn
            with _db_conn.cursor() as _cur:
                _cur.execute('PRAGMA wal_checkpoint(TRUNCATE);')
        except Exception:
            pass
    except InterruptedError:
        # User cancelled — the cancel view already wrote CANCELLED status; nothing to do
        pass
    except Exception as exc:
        try:
            run.status  = SimulationRun.ERROR
            run.message = str(exc)
            run.finished_at = timezone.now()
            run.save(update_fields=['status', 'message', 'finished_at'])
        except Exception:
            pass


def run_simulation(request, sim_id):
    """POST: create a SimulationRun, redirect immediately, run in background process."""
    from django.shortcuts import get_object_or_404, redirect
    from django.contrib import messages
    from multiprocessing import get_context
    from django.db import connections
    if request.method == 'POST':
        sim = get_object_or_404(Simulation, pk=sim_id)
        run = SimulationRun.objects.create(simulation=sim, status=SimulationRun.PENDING)
        messages.success(request, f'Simulation \u201c{sim.name}\u201d started.')
        # Pre-set status to RUNNING so the poll endpoint shows activity
        # immediately.  On Windows, the 'spawn' subprocess takes 10–30 s to
        # cold-import Django + numpy + h5py before it can set its own status,
        # causing the UI to appear frozen.  The worker will overwrite this
        # with its own started_at timestamp once it is actually running.
        from django.utils import timezone as _tz
        run.status = SimulationRun.RUNNING
        run.message = 'Starting simulation process\u2026'
        run.started_at = _tz.now()
        run.save(update_fields=['status', 'message', 'started_at'])
        # Close inherited DB handles before child process starts.
        connections.close_all()
        from dashboard.simulation_worker import run as _worker_run
        p = get_context('spawn').Process(target=_worker_run, args=(run.pk,), daemon=True)
        p.start()
    return redirect('dashboard-simulation-detail', sim_id=sim_id)


def _simulation_process_entry(run_id):
    """Entry point for the spawned simulation process.

    A 'spawn' child process starts with a clean interpreter — Django is not
    yet configured.  We must call django.setup() before importing any models
    or running application code.
    """
    import os
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'r2h2_ui.settings')
    django.setup()
    _run_simulation_thread(run_id)


def _fmt_duration(seconds):
    """Format a duration in seconds as hh:mm:ss."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f'{h:02d}:{m:02d}:{sec:02d}'


_DEFAULT_1HZ_PLOT_KEYS = []


def _get_run_output_abspath(run):
    """Return the absolute output path for a run, or None if unavailable."""
    from django.conf import settings as dj_settings
    from pathlib import Path

    if not run.output_path:
        return None

    abs_path = Path(dj_settings.MEDIA_ROOT) / run.output_path
    return abs_path if abs_path.exists() else None


def _get_run_1hz_info(run):
    """Return lightweight 1Hz availability metadata for a completed run."""
    abs_path = _get_run_output_abspath(run)
    if abs_path is None:
        return {'has_1hz_data': False, 'output_abspath': None}

    import h5py

    try:
        with h5py.File(abs_path, 'r') as h5_file:
            has_1hz_data = 'time_series_1hz' in h5_file
    except OSError:
        has_1hz_data = False

    return {
        'has_1hz_data': has_1hz_data,
        'output_abspath': abs_path,
    }


def _parse_1hz_request_time(raw_value):
    """Parse an ISO datetime string from the browser into epoch seconds."""
    if not raw_value:
        return None

    import pandas as pd

    parsed = pd.to_datetime(raw_value, utc=True, errors='coerce')
    if pd.isna(parsed):
        raise ValueError('Invalid datetime range.')
    return int(parsed.timestamp())


def _select_1hz_plot_keys(ts_group):
    """Return plottable saved 1Hz dataset keys.

    Only 1-D datasets other than ``time_indices`` are considered plottable.
    The saved HDF5 content is already restricted to user-selected variables.
    """
    plot_keys = []
    for key in sorted(ts_group.keys()):
        if key == 'time_indices':
            continue
        ds = ts_group[key]
        if getattr(ds, 'ndim', None) == 1:
            plot_keys.append(key)
    return plot_keys


def _get_run_datetime_origin(run):
    """Return the datetime origin used by the hourly charts for this run."""
    import datetime as _dt

    if run.run_start_date:
        return _dt.datetime.combine(run.run_start_date, _dt.time.min, tzinfo=_dt.timezone.utc)

    sim = run.simulation
    datum = sim.datum_date or _dt.date.today()
    datum_start = _dt.date(datum.year, 1, 1)
    return _dt.datetime.combine(datum_start, _dt.time.min, tzinfo=_dt.timezone.utc)


def _resolve_1hz_time_seconds(run, raw_time_seconds, *, start_hour):
    """Convert saved 1Hz time indices to absolute epoch seconds.

    Newer simulation outputs store sequential seconds starting at zero for the
    collected 1Hz window. Older or manually-produced files may already contain
    absolute epoch seconds, so preserve those when detected.
    """
    import numpy as np

    if raw_time_seconds.size == 0:
        return raw_time_seconds

    first_value = int(raw_time_seconds[0])
    if first_value >= 946684800:
        return raw_time_seconds

    origin_dt = _get_run_datetime_origin(run)
    origin_seconds = int(origin_dt.timestamp()) + int(start_hour) * 3600
    return np.asarray(raw_time_seconds, dtype=np.int64) + origin_seconds


def _load_run_1hz_plot_data(run, *, start_iso=None, end_iso=None, max_points=4000):
    """Load 1Hz time-series data for browser plotting, adapting to a visible time window."""
    info = _get_run_1hz_info(run)
    if not info['has_1hz_data'] or info['output_abspath'] is None:
        return None

    import math
    import h5py
    import numpy as np
    import pandas as pd

    with h5py.File(info['output_abspath'], 'r') as h5_file:
        ts_group = h5_file['time_series_1hz']
        if 'time_indices' not in ts_group:
            return None

        available_keys = sorted(ts_group.keys())
        plot_keys = _select_1hz_plot_keys(ts_group)
        start_hour = int(ts_group.attrs.get('start_hour', 0))
        end_hour = int(ts_group.attrs.get('end_hour', 0))

        raw_time_seconds = np.asarray(ts_group['time_indices'], dtype=np.int64)
        time_seconds = _resolve_1hz_time_seconds(run, raw_time_seconds, start_hour=start_hour)
        total_points = int(time_seconds.shape[0])
        if total_points == 0:
            return {
                'x_values': [],
                'series': [],
                'available_keys': available_keys,
                'plot_keys': plot_keys,
                'points_total': 0,
                'window_points': 0,
                'points_shown': 0,
                'downsample_step': 1,
                'is_full_resolution': True,
                'window_start': None,
                'window_end': None,
                'full_start': None,
                'full_end': None,
                'start_hour': start_hour,
                'end_hour': end_hour,
                'output_name': info['output_abspath'].name,
            }

        full_start = pd.to_datetime(time_seconds[0], unit='s', utc=True).strftime('%Y-%m-%dT%H:%M:%SZ')
        full_end = pd.to_datetime(time_seconds[-1], unit='s', utc=True).strftime('%Y-%m-%dT%H:%M:%SZ')

        range_start_seconds = _parse_1hz_request_time(start_iso)
        range_end_seconds = _parse_1hz_request_time(end_iso)
        if range_start_seconds is not None and range_end_seconds is not None and range_end_seconds < range_start_seconds:
            raise ValueError('Invalid datetime range.')

        start_idx = 0
        end_idx = total_points
        if range_start_seconds is not None:
            start_idx = int(np.searchsorted(time_seconds, range_start_seconds, side='left'))
        if range_end_seconds is not None:
            end_idx = int(np.searchsorted(time_seconds, range_end_seconds, side='right'))

        start_idx = max(0, min(start_idx, total_points))
        end_idx = max(start_idx, min(end_idx, total_points))

        window_seconds = time_seconds[start_idx:end_idx]
        window_points = int(window_seconds.shape[0])
        if window_points == 0:
            return {
                'x_values': [],
                'series': [],
                'available_keys': available_keys,
                'plot_keys': plot_keys,
                'points_total': total_points,
                'window_points': 0,
                'points_shown': 0,
                'downsample_step': 1,
                'is_full_resolution': True,
                'window_start': start_iso or full_start,
                'window_end': end_iso or full_end,
                'full_start': full_start,
                'full_end': full_end,
                'start_hour': start_hour,
                'end_hour': end_hour,
                'output_name': info['output_abspath'].name,
            }

        downsample_step = max(1, int(math.ceil(window_points / max_points)))
        x_values = pd.to_datetime(window_seconds[::downsample_step], unit='s', utc=True).strftime('%Y-%m-%dT%H:%M:%SZ').tolist()

        series = []
        for key in plot_keys:
            y_values = np.asarray(ts_group[key][start_idx:end_idx])
            if y_values.ndim == 2:
                y_values = y_values[:, 0]
            if y_values.ndim != 1 or y_values.shape[0] != window_points:
                continue
            series.append({
                'key': key,
                'y': np.asarray(y_values[::downsample_step], dtype=float).tolist(),
            })

    return {
        'x_values': x_values,
        'series': series,
        'available_keys': available_keys,
        'plot_keys': plot_keys,
        'points_total': total_points,
        'window_points': window_points,
        'points_shown': len(x_values),
        'downsample_step': downsample_step,
        'is_full_resolution': downsample_step == 1,
        'window_start': pd.to_datetime(window_seconds[0], unit='s', utc=True).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'window_end': pd.to_datetime(window_seconds[-1], unit='s', utc=True).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'full_start': full_start,
        'full_end': full_end,
        'start_hour': start_hour,
        'end_hour': end_hour,
        'output_name': info['output_abspath'].name,
    }


def _get_run_1hz_meta(run):
    """Read lightweight metadata from the 1Hz HDF5 file without loading data arrays."""
    info = _get_run_1hz_info(run)
    if not info['has_1hz_data'] or info['output_abspath'] is None:
        return None
    import h5py
    try:
        with h5py.File(info['output_abspath'], 'r') as h5_file:
            if 'time_series_1hz' not in h5_file:
                return None
            ts_group = h5_file['time_series_1hz']
            if 'time_indices' not in ts_group:
                return None
            plot_keys = _select_1hz_plot_keys(ts_group)
            start_hour = int(ts_group.attrs.get('start_hour', 0))
            end_hour = int(ts_group.attrs.get('end_hour', 0))
            points_total = int(ts_group['time_indices'].shape[0])
    except Exception:
        return None
    if points_total <= 0 or not plot_keys:
        return None
    return {
        'points_total': points_total,
        'n_channels': len(plot_keys),
        'start_hour': start_hour,
        'end_hour': end_hour,
        'output_name': info['output_abspath'].name,
    }


def _compute_eta_iso(checkpoints, total_hours):
    """Compute an ETA ISO string from progress checkpoints.

    Each checkpoint is {"t": epoch_float, "h": sim_hours_done}.
    Consecutive pairs give a sim-hours/sec rate for that chunk.
    Chunks where the rate is less than half the median are treated as
    outliers (e.g. system hibernated during that window) and excluded.
    Returns an ISO-8601 UTC string, or None if insufficient data.
    """
    import statistics
    import time as _time

    if len(checkpoints) < 2:
        return None

    # Compute per-chunk rates
    rates = []
    for i in range(1, len(checkpoints)):
        dt = checkpoints[i]['t'] - checkpoints[i - 1]['t']
        dh = checkpoints[i]['h'] - checkpoints[i - 1]['h']
        if dt > 0 and dh >= 0:
            rates.append(dh / dt)  # sim-hours per wall-clock second

    if not rates:
        return None

    # Filter outliers: exclude any chunk whose rate is < half the median
    # (catches hibernation/sleep gaps where wall time elapsed but sim did not advance)
    median_rate = statistics.median(rates)
    if median_rate <= 0:
        return None
    clean_rates = [r for r in rates if r >= median_rate / 2]
    if not clean_rates:
        return None

    avg_rate = statistics.mean(clean_rates)  # sim-hours / wall-sec
    if avg_rate <= 0:
        return None

    hours_done = checkpoints[-1]['h']
    hours_remaining = total_hours - hours_done
    if hours_remaining <= 0:
        return None

    secs_remaining = hours_remaining / avg_rate
    # Anchor to current wall time (not start time — robust to hibernation)
    finish_epoch = _time.time() + secs_remaining
    from datetime import datetime, timezone as _tz
    return datetime.fromtimestamp(finish_epoch, tz=_tz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def poll_simulation_run(request, sim_id, run_id):
    """GET: return JSON status of a SimulationRun for client-side polling."""
    import re
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    from django.utils import timezone
    run = get_object_or_404(SimulationRun, pk=run_id, simulation_id=sim_id)
    run_1hz_info = _get_run_1hz_info(run) if run.output_path else {'has_1hz_data': False}
    dur = run.duration_seconds
    if dur is not None:
        duration_str = _fmt_duration(dur)
    elif run.started_at is not None:
        elapsed = (timezone.now() - run.started_at).total_seconds()
        duration_str = _fmt_duration(elapsed)
    else:
        duration_str = ''

    # Extract hours_done / total_hours / pct from progress message
    hours_done = None
    total_hours = None
    pct = None
    msg = run.message or ''
    m = re.search(r'hour (\d+)/(\d+)', msg)
    if m:
        hours_done  = int(m.group(1))
        total_hours = int(m.group(2))
    m2 = re.search(r'(\d+)\xa0?%', msg)
    if m2:
        pct = int(m2.group(1))

    # Compute robust ETA from checkpoints stored by _on_progress.
    # Fall back to the persisted est_finish_at if checkpoints are sparse.
    checkpoints = run.progress_checkpoints or []
    eta_iso = None
    estimated_total_sec = None
    if total_hours and checkpoints:
        eta_iso = _compute_eta_iso(checkpoints, total_hours)
        if eta_iso and hours_done and hours_done > 0 and len(checkpoints) >= 2:
            import time as _time
            from datetime import datetime, timezone as _tz
            finish_epoch = datetime.strptime(eta_iso, '%Y-%m-%dT%H:%M:%SZ').replace(
                tzinfo=_tz.utc).timestamp()
            estimated_total_sec = finish_epoch - checkpoints[0]['t']
    # If live computation not possible, serve the last persisted estimate
    if not eta_iso and run.est_finish_at:
        from datetime import datetime, timezone as _tz
        eta_iso = run.est_finish_at.strftime('%Y-%m-%dT%H:%M:%SZ')
    # Use persisted pct as fallback if message parse failed
    if pct is None and run.progress_pct is not None:
        pct = run.progress_pct

    # elapsed_seconds: derived from checkpoints (most recent wall time), avoids
    # start_time which may be wrong after hibernation
    elapsed_seconds = None
    current_elapsed = None
    if run.started_at is not None:
        import time as _time
        if checkpoints:
            # Use wall-clock delta between first and last checkpoint + elapsed at first checkpoint
            # Actually just show seconds since started_at; this is only for the duration ticker
            current_elapsed = (timezone.now() - run.started_at).total_seconds()
        else:
            current_elapsed = (timezone.now() - run.started_at).total_seconds()
        elapsed_seconds = current_elapsed

    return JsonResponse({
        'status':               run.status,
        'message':              msg,
        'duration':             duration_str,
        'elapsed_seconds':      elapsed_seconds,
        'current_elapsed':      current_elapsed,
        'hours_done':           hours_done,
        'total_hours':          total_hours,
        'pct':                  pct,
        'eta_iso':              eta_iso,           # ISO UTC string for frontend localisation
        'estimated_total_sec':  estimated_total_sec,  # for frontend poll-interval decision
        'output_path':          run.output_path or '',
        'has_1hz_data':         run_1hz_info['has_1hz_data'],
        'done':                 run.status in (SimulationRun.DONE, SimulationRun.ERROR, SimulationRun.CANCELLED),
    })


@require_POST
def cancel_simulation_run(request, sim_id, run_id):
    """POST: mark a pending/running SimulationRun as cancelled (error status)."""
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    from django.utils import timezone
    run = get_object_or_404(SimulationRun, pk=run_id, simulation_id=sim_id)
    if run.status in (SimulationRun.PENDING, SimulationRun.RUNNING):
        run.status = SimulationRun.CANCELLED
        run.message = 'Cancelled by user.'
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'message', 'finished_at'])
    return JsonResponse({'status': run.status, 'message': run.message})


@require_POST
def delete_simulation_run(request, sim_id, run_id):
    """POST: delete a single finished SimulationRun."""
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    run = get_object_or_404(SimulationRun, pk=run_id, simulation_id=sim_id)
    # Refuse to delete an active run — cancel it first
    if run.status in (SimulationRun.PENDING, SimulationRun.RUNNING):
        return JsonResponse({'error': 'Cannot delete an active run. Cancel it first.'}, status=400)
    # Remove the HDF5 output file if it exists
    if run.output_path:
        from django.conf import settings as dj_settings
        from pathlib import Path
        abs_path = Path(dj_settings.MEDIA_ROOT) / run.output_path
        try:
            abs_path.unlink(missing_ok=True)
        except Exception:
            pass
    run.delete()
    return JsonResponse({'deleted': run_id})


@require_POST
def update_run_description(request, sim_id, run_id):
    """POST: save a user-supplied description for a SimulationRun."""
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    run = get_object_or_404(SimulationRun, pk=run_id, simulation_id=sim_id)
    desc = request.POST.get('description', '').strip()[:200]
    run.description = desc
    # Refresh the completion message to reflect the new description
    if run.status == SimulationRun.DONE:
        if desc:
            suffix = desc if len(desc) <= 80 else desc[:77] + '\u2026'
            run.message = f'Completed. {suffix}'
        else:
            run.message = f'Simulation \u201c{run.simulation.name}\u201d completed successfully.'
        run.save(update_fields=['description', 'message'])
    else:
        run.save(update_fields=['description'])
    return JsonResponse({'description': run.description, 'message': run.message})


def download_run_output(request, sim_id, run_id):
    """GET: serve the HDF5 output file with correct Content-Type so browsers
    don't append an extra .html suffix (Safari issue)."""
    from django.http import FileResponse, Http404
    from django.shortcuts import get_object_or_404
    run = get_object_or_404(SimulationRun, pk=run_id, simulation_id=sim_id)
    if not run.output_path:
        raise Http404('No output file for this run.')

    abs_path = _get_run_output_abspath(run)
    if abs_path is None:
        raise Http404('Output file not found.')

    response = FileResponse(
        open(abs_path, 'rb'),
        content_type='application/x-hdf5',
        as_attachment=True,
        filename=abs_path.name,
    )
    return response


def view_run_results(request, sim_id, run_id):
    """GET: display interactive charts for a completed SimulationRun."""
    from django.shortcuts import get_object_or_404
    import json as _json

    run = get_object_or_404(SimulationRun, pk=run_id, simulation_id=sim_id)
    sim = run.simulation

    if not run.output_path:
        from django.contrib import messages
        messages.error(request, 'No results file found for this run.')
        return redirect('dashboard-simulation-detail', sim_id=sim_id)

    abs_path = _get_run_output_abspath(run)
    if abs_path is None:
        from django.contrib import messages
        messages.error(request, f'Results file not found: {run.output_path}')
        return redirect('dashboard-simulation-detail', sim_id=sim_id)

    import h5py
    import numpy as np

    years_data = []      # list of dicts, one per year group
    meta_info = {}

    with h5py.File(abs_path, 'r') as f:
        # Read metadata attrs
        if 'meta' in f:
            m = f['meta']
            meta_info = {k: (v.item() if hasattr(v, 'item') else v)
                         for k, v in m.attrs.items()}

        # Collect sorted year groups
        year_keys = sorted(
            [k for k in f.keys() if k.startswith('year_')],
            key=lambda s: int(s.split('_')[1])
        )

        for yk in year_keys:
            yr_grp = f[yk]
            ydata = {'label': f'Year {int(yk.split("_")[1]) + 1}'}

            # Battery
            bat = yr_grp.get('battery', {})
            
            # Extract scalar attributes
            if bat:
                if 'iNumReplacements' in bat.attrs:
                    ydata['iNumReplacements'] = int(bat.attrs['iNumReplacements'])
                if 'iNumReplacementsYear' in bat.attrs:
                    ydata['iNumReplacementsYear'] = int(bat.attrs['iNumReplacementsYear'])
                if 'iNumReplacementsCumulative' in bat.attrs:
                    ydata['iNumReplacementsCumulative'] = int(bat.attrs['iNumReplacementsCumulative'])
                if 'rFinalBatteryRating' in bat.attrs:
                    ydata['rFinalBatteryRating'] = float(bat.attrs['rFinalBatteryRating'])
            
            for key in ('arSoc', 'arSocMax', 'arSocMin', 'arSocAv', 'arRCD', 'arBatteryRating'):
                if key in bat:
                    arr = bat[key][:]
                    # Downsample to at most 8760 points to keep JSON small
                    if len(arr) > 8760:
                        step = len(arr) // 8760
                        arr = arr[::step]
                    ydata[key] = arr.tolist()
            
            # Spill power (keep full resolution for integration)
            if 'arSpillPower' in bat:
                arr = bat['arSpillPower'][:]
                ydata['arSpillPowerFull'] = arr.tolist()  # full resolution for total calculation
                # Also add downsampled for optional charting
                if len(arr) > 8760:
                    step = len(arr) // 8760
                    arr = arr[::step]
                ydata['arSpillPower'] = arr.tolist()

            # Electrolyser
            elec = yr_grp.get('electrolyser', {})
            if 'arElecOnAv' in elec:
                arr = elec['arElecOnAv'][:]
                if len(arr) > 8760:
                    arr = arr[:8760]
                ydata['arElecOnAv'] = arr.tolist()
                ydata['iNumUnits'] = int(elec.attrs.get('iNumUnits', 0))
            if 'arEtaElPeak' in elec:
                arr = elec['arEtaElPeak'][:]
                if len(arr) > 8760:
                    arr = arr[:8760]
                ydata['arEtaElPeak'] = arr.tolist()
            if 'arEtaSystemPeak' in elec:
                arr = elec['arEtaSystemPeak'][:]
                if len(arr) > 8760:
                    arr = arr[:8760]
                ydata['arEtaSystemPeak'] = arr.tolist()
            if 'arHourlyDegradation' in elec:
                deg = elec['arHourlyDegradation'][:]
                if deg.ndim == 1:
                    # Single unit — wrap in outer list
                    units = [deg[:8760].tolist()]
                else:
                    # 2-D [n_units, n_hours]
                    units = [deg[u, :8760].tolist() for u in range(deg.shape[0])]
                ydata['arDegradationPerUnit'] = units

            # Power traces
            pwr_grp = yr_grp.get('power', {})
            for key, out_key in (
                ('arWindPowerFilt',      'arWindPower'),
                ('arWindSpeed',          'arWindSpeed'),
                ('arAvailablePower',     'arAvailablePower'),
                ('arTotalElectroDemand', 'arElectroPower'),
            ):
                if key in pwr_grp:
                    arr = pwr_grp[key][:]
                    if arr.ndim > 1:
                        arr = arr.mean(axis=0)
                    if len(arr) > 8760:
                        arr = arr[:8760]
                    ydata[out_key] = arr.tolist()

            # H2
            h2g = yr_grp.get('h2', {})
            if 'arTotalH2' in h2g:
                arr = h2g['arTotalH2'][:]
                if len(arr) > 8760:
                    arr = arr[:8760]
                ydata['arTotalH2'] = arr.tolist()

            years_data.append(ydata)

    # Legacy fallback: append per-year wind speed from currently linked
    # WindInput files only for outputs that do not already carry arWindSpeed.
    try:
        missing_windspeed = any(not yd.get('arWindSpeed') for yd in years_data)
        if missing_windspeed:
            import datetime as _dt_mod
            wind_paths = _resolve_wind_h5_paths(sim)
            ws_full = []
            for wp in wind_paths:
                with h5py.File(wp, 'r') as wf:
                    if 'WindSpeed' in wf:
                        # WindSpeed is expected to be hourly and may be stored as
                        # (hours,), (1, hours), or a higher-rank array depending
                        # on source tooling. Flatten in file order so the UI sees
                        # the same chronological series used by the simulation.
                        ws_arr = np.asarray(wf['WindSpeed'][:], dtype=float)
                        if ws_arr.size:
                            ws_full.extend(ws_arr.reshape(-1).tolist())

            if ws_full:
                # Determine the start-hour offset into the full wind array
                _ws_start_hour = 0
                if run.run_start_date:
                    datum = sim.datum_date or _dt_mod.date(run.run_start_date.year, 1, 1)
                    _ws_start_hour = max(0, int((run.run_start_date - datum).days * 24))

                ws_sliced = ws_full[_ws_start_hour:]

                offset = 0
                for yd in years_data:
                    if yd.get('arWindSpeed'):
                        offset += len(yd.get('arWindSpeed') or [])
                        continue
                    ref = (yd.get('arSoc') or yd.get('arTotalH2') or yd.get('arElecOnAv') or [])
                    n = len(ref)
                    yd['arWindSpeed'] = ws_sliced[offset: offset + n] if n else []
                    offset += n
    except Exception:
        pass  # wind data is optional — silently skip if unavailable

    # Compute per-year cumulative hour offsets from datum.
    # If the run used a specific start date, use that as the x-axis origin;
    # otherwise fall back to 1 Jan of the datum year (integer-based origin).
    from datetime import date as _date
    if run.run_start_date:
        datum_iso = run.run_start_date.isoformat()
    else:
        datum = sim.datum_date or _date.today()
        datum_iso = _date(datum.year, 1, 1).isoformat()
    year_cumulative_hours = []
    cumulative = 0
    for yd in years_data:
        year_cumulative_hours.append(cumulative)
        ref = (yd.get('arSoc') or yd.get('arTotalH2') or yd.get('arElecOnAv') or [])
        cumulative += len(ref)

    # 1Hz inline preview — load lightweight metadata only; actual data fetched async by browser
    run_1hz_info = _get_run_1hz_info(run)
    has_1hz_data = run_1hz_info['has_1hz_data']
    hz_meta = {}
    if has_1hz_data:
        _hz_meta = _get_run_1hz_meta(run)
        if _hz_meta:
            hz_meta = _hz_meta
        else:
            has_1hz_data = False

    context = {
        'run': run,
        'sim': sim,
        'meta_info': meta_info,
        'years_data_json': _json.dumps(years_data),
        'year_cumulative_hours_json': _json.dumps(year_cumulative_hours),
        'datum_iso': datum_iso,
        'n_years': len(years_data),
        'n_years_range': range(len(years_data)),
        'xaxis_datetime': run.xaxis_datetime,
        'update_xaxis_url': f'/simulations/{sim_id}/run/{run_id}/xaxis/',
        'has_wind': sim.wind_inputs.exists(),
        'has_1hz_data': has_1hz_data,
        'hz_meta': hz_meta,
        'hz_channel_range': range(hz_meta.get('n_channels', 0)),
        'run_1hz_url': f'/simulations/{sim_id}/run/{run_id}/1hz-json/',
    }
    return render(request, 'dashboard/run_results.html', context)


def run_1hz_json(request, sim_id, run_id):
    """GET: Return 1Hz time-series data as JSON for async browser loading.

    Query params:
        start       ISO datetime string – window start (optional)
        end         ISO datetime string – window end (optional)
        max_points  int – max data points to return (default 2000, cap 100000)
    """
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    run = get_object_or_404(SimulationRun, pk=run_id, simulation_id=sim_id)
    start_iso = request.GET.get('start') or None
    end_iso   = request.GET.get('end')   or None
    try:
        max_pts = int(request.GET.get('max_points', 2000))
        max_pts = max(100, min(max_pts, 100_000))
    except (ValueError, TypeError):
        max_pts = 2000
    try:
        pd = _load_run_1hz_plot_data(run, start_iso=start_iso, end_iso=end_iso, max_points=max_pts)
    except ValueError as e:
        return JsonResponse({'error': str(e)}, status=400)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
    if pd is None:
        return JsonResponse({'error': 'No 1Hz data available.'}, status=404)
    if not pd['series']:
        return JsonResponse({'error': 'No saved 1Hz channels available for plotting.'}, status=404)
    return JsonResponse({
        'x_values':          pd['x_values'],
        'series':            pd['series'],
        'full_start':        pd['full_start'],
        'full_end':          pd['full_end'],
        'points_total':      pd['points_total'],
        'points_shown':      pd['points_shown'],
        'downsample_step':   pd['downsample_step'],
        'is_full_resolution': pd['is_full_resolution'],
    })


@require_POST
def update_sim_datum(request, sim_id):
    """POST: save datum_date for a Simulation (used as datetime axis origin in results)."""
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    raw = request.POST.get('datum_date', '').strip()
    sim = get_object_or_404(Simulation, pk=sim_id)
    if raw == '':
        sim.datum_date = None
    else:
        from datetime import date as _date
        try:
            sim.datum_date = _date.fromisoformat(raw)
        except ValueError:
            return JsonResponse({'error': 'Invalid date format (expected YYYY-MM-DD).'}, status=400)
    sim.save(update_fields=['datum_date'])
    display = sim.datum_date.strftime('%d %b %Y') if sim.datum_date else None
    return JsonResponse({'datum_date': sim.datum_date.isoformat() if sim.datum_date else '',
                         'datum_display': display})


@require_POST
def update_run_xaxis(request, sim_id, run_id):
    """POST: toggle datetime vs hours x-axis preference for a run's results charts."""
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    run = get_object_or_404(SimulationRun, pk=run_id, simulation_id=sim_id)
    val = request.POST.get('xaxis_datetime', 'true').strip().lower()
    run.xaxis_datetime = (val == 'true')
    run.save(update_fields=['xaxis_datetime'])
    return JsonResponse({'xaxis_datetime': run.xaxis_datetime})


@require_POST
def update_sim_date_range(request, sim_id):
    """POST: save start date and end date for a Simulation.
    Derives duration_days from (end_date - start_date).
    Validates that dates are within the allowed range from wind data.
    """
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    from datetime import date as _date
    start_raw = request.POST.get('start_date', '').strip()
    end_raw   = request.POST.get('end_date',   '').strip()
    sim = get_object_or_404(Simulation, pk=sim_id)
    try:
        start = _date.fromisoformat(start_raw) if start_raw else None
        end   = _date.fromisoformat(end_raw)   if end_raw   else None
    except ValueError:
        return JsonResponse({'error': 'Invalid date format (expected YYYY-MM-DD).'}, status=400)

    # Validation
    if start and sim.datum_date and start < sim.datum_date:
        return JsonResponse({'error': f'Start date cannot be before the axis origin date ({sim.datum_date.strftime("%d %b %Y")}).'}, status=400)
    
    max_end_date = sim.get_max_end_date()
    if end and max_end_date and end > max_end_date:
        return JsonResponse({'error': f'End date cannot be after the maximum possible date from wind data ({max_end_date.strftime("%d %b %Y")}).'}, status=400)

    sim.start_date = start
    sim.end_date = end
    if start and end and end >= start:
        sim.duration_days = (end - start).days + 1  # end_date inclusive: days in range
    else:
        sim.duration_days = None
    sim.save(update_fields=['start_date', 'end_date', 'duration_days'])
    return JsonResponse({
        'start_date':    sim.start_date.isoformat() if sim.start_date else '',
        'end_date':      sim.end_date.isoformat() if sim.end_date else '',
        'duration_days': sim.duration_days,
    })


@require_POST
def update_sim_1hz(request, sim_id):
    """POST: save 1Hz data collection settings for a Simulation."""
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    from django.core.exceptions import ValidationError
    from datetime import date as _date

    sim = get_object_or_404(Simulation, pk=sim_id)

    update_fields = []

    # Handle collect_1hz_data toggle
    if 'collect_1hz_data' in request.POST:
        collect_enabled = request.POST.get('collect_1hz_data', '0').strip() == '1'
        sim.collect_1hz_data = collect_enabled
        update_fields.append('collect_1hz_data')

    # Handle date range (may be in the same request as the toggle)
    start_raw = request.POST.get('collect_1hz_start_date', None)
    end_raw   = request.POST.get('collect_1hz_end_date',   None)

    if start_raw is not None or end_raw is not None:
        try:
            start = _date.fromisoformat(start_raw.strip()) if start_raw and start_raw.strip() else None
            end   = _date.fromisoformat(end_raw.strip())   if end_raw   and end_raw.strip()   else None
        except ValueError:
            return JsonResponse({'error': 'Invalid date format (expected YYYY-MM-DD).'}, status=400)

        # If enabling collection and no dates supplied, fall back to defaults
        if sim.collect_1hz_data and not start and not end and not sim.collect_1hz_start_date:
            start, end = _default_1hz_range(sim)

        sim.collect_1hz_start_date = start
        sim.collect_1hz_end_date   = end
        update_fields += ['collect_1hz_start_date', 'collect_1hz_end_date']

    # Handle hz_variables list (JSON array of variable name strings)
    hz_vars_raw = request.POST.get('hz_variables', None)
    if hz_vars_raw is not None:
        import json as _json_mod
        try:
            hz_vars = _json_mod.loads(hz_vars_raw)
        except (ValueError, TypeError):
            # Accept comma-separated fallback
            hz_vars = [v.strip() for v in hz_vars_raw.split(',') if v.strip()]
        if not isinstance(hz_vars, list):
            return JsonResponse({'error': 'hz_variables must be a JSON array.'}, status=400)
        # Sanitise: only allow safe identifier-like strings
        import re as _re
        hz_vars = [v for v in hz_vars if isinstance(v, str) and _re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', v)]
        sim.hz_variables = hz_vars
        update_fields.append('hz_variables')

    if not update_fields:
        return JsonResponse({'error': 'No valid parameters provided.'}, status=400)

    sim.save(update_fields=update_fields)
    return JsonResponse({
        'collect_1hz_data':       sim.collect_1hz_data,
        'collect_1hz_start_date': sim.collect_1hz_start_date.isoformat() if sim.collect_1hz_start_date else '',
        'collect_1hz_end_date':   sim.collect_1hz_end_date.isoformat()   if sim.collect_1hz_end_date   else '',
        'hz_variables':           sim.hz_variables or [],
    })


def sim_1hz_variables(request, sim_id):
    """GET: Return the list of available t_out variable names for 1Hz capture.

    If the simulation has a linked controller, parse the Python source to find
    all ``t_out.<name>`` assignments where <name> looks like an array attribute.
    Always include the built-in TimeOutputs array attributes too.
    """
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    import re as _re

    sim = get_object_or_404(Simulation, pk=sim_id)

    # Built-in 1-D t_out array attributes from TimeOutputs (arrays of length T)
    _BUILTIN = [
        'arWindPowerFilt',
        'arAvailablePower',
        'arElectroAvailablePowerA',
        'arElectroAvailablePower',
        'arTotalElectroDemand',
        'arTotalElectroOn',
        'arP_el_total',
        'arT_stack',
        'arH2Dot_total',
        'arV_cell_avg',
        'arEta_el_total',
        'arEta_system_total',
        'arQ_gain_total',
        'arQ_lost_total',
        'arQ_cool_total',
        'arP_cool_elec_total',
    ]

    def _parse_buffer_aliases_from_source(src_text):
        """Parse `buffer = {'alias': battery.arSoC, ...}` aliases from controller code."""
        import ast as _ast

        aliases = []
        try:
            tree = _ast.parse(src_text)
        except Exception:
            return aliases

        def _dict_from_node(value_node):
            if not isinstance(value_node, _ast.Dict):
                return
            for k_node, v_node in zip(value_node.keys, value_node.values):
                if not isinstance(k_node, _ast.Constant) or not isinstance(k_node.value, str):
                    continue
                # Support battery.<attr> or t_out.<attr> sources
                if (
                    isinstance(v_node, _ast.Attribute)
                    and isinstance(v_node.value, _ast.Name)
                    and v_node.value.id in ('battery', 't_out')
                ):
                    aliases.append(k_node.value)

        for node in _ast.walk(tree):
            if isinstance(node, _ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, _ast.Name) and tgt.id == 'buffer':
                        _dict_from_node(node.value)
            elif isinstance(node, _ast.AnnAssign):
                if isinstance(node.target, _ast.Name) and node.target.id == 'buffer':
                    _dict_from_node(node.value)

        # Keep stable order and uniqueness
        out = []
        seen = set()
        for a in aliases:
            if a not in seen:
                seen.add(a)
                out.append(a)
        return out

    # Parse controller source for additional t_out.* array assignments and
    # aliases defined in a `buffer` dict.
    controller_vars = []
    buffer_aliases = []
    if sim.controller and sim.controller.file:
        try:
            from r2h2.config import get_controllers_dir
            import pathlib
            ctrl_path = pathlib.Path(get_controllers_dir()) / sim.controller.file.name
            if ctrl_path.exists():
                src = ctrl_path.read_text(encoding='utf-8', errors='replace')
                buffer_aliases = _parse_buffer_aliases_from_source(src)
                # Match t_out.<identifier> = ... where identifier starts with 'ar'
                found = _re.findall(r'\bt_out\.([a-zA-Z_][a-zA-Z0-9_]*)\s*=', src)
                # Keep only array-like names (starting with 'ar') not already in built-ins
                for name in found:
                    if name.startswith('ar') and name not in _BUILTIN and name not in controller_vars:
                        controller_vars.append(name)
        except Exception:
            pass

    all_vars = _BUILTIN + controller_vars + [a for a in buffer_aliases if a not in _BUILTIN and a not in controller_vars]
    return JsonResponse({
        'available': all_vars,
        'controller_extra': controller_vars + buffer_aliases,
        'selected': sim.hz_variables or [],
    })


@require_POST
def update_sim_duration(request, sim_id):
    """POST: save duration_days override for a Simulation.
    When duration_days is cleared (mode='all'), also clear start_date/end_date
    so the simulation uses the full wind dataset.
    """
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    raw = request.POST.get('duration_days', '').strip()
    sim = get_object_or_404(Simulation, pk=sim_id)
    if raw == '' or raw is None:
        sim.duration_days = None
        sim.start_date = None
        sim.end_date = None
        sim.save(update_fields=['duration_days', 'start_date', 'end_date'])
    else:
        try:
            days = int(raw)
            sim.duration_days = max(1, days)
        except ValueError:
            return JsonResponse({'error': 'Invalid value.'}, status=400)
        sim.save(update_fields=['duration_days'])
    return JsonResponse({'duration_days': sim.duration_days})







# ---------------------------------------------------------------------------
# Engineering controller management
# ---------------------------------------------------------------------------

def _ensure_default_controller():
    """Idempotently create the built-in default Controller DB record.

    Creates (or refreshes) the 'default_controller.py' record.  The physical
    file is seeded by get_controllers_dir() the first time the controllers
    directory is accessed, so we only need to ensure the DB row exists.
    """
    from .models import Controller
    from r2h2.config import get_controllers_dir
    import datetime
    # Ensure the file exists on disk (get_controllers_dir seeds it)
    ctrl_dir = get_controllers_dir()
    default_path = ctrl_dir / 'default_controller.py'
    Controller.objects.get_or_create(
        file='default_controller.py',
        defaults={
            'name':         'Default Controller',
            'description':  'Built-in template controller provided with R2H2. '
                            'Copy and rename before modifying.',
            'author':       'R2H2',
            'date_created': datetime.date.today(),
            'verified':     True,
        },
    )


def _list_controller_files():
    """Return sorted list of .py filenames in the controllers directory."""
    from r2h2.config import get_controllers_dir
    ctrl_dir = get_controllers_dir()
    return sorted(p.name for p in ctrl_dir.glob('*.py'))


# Patterns that are flagged as warnings on save (not blocked — desktop app).
# Each entry: (regex_pattern, human-readable reason)
_CTRL_DANGEROUS_PATTERNS = [
    (r'\bsubprocess\b',          'subprocess: can execute arbitrary OS commands'),
    (r'\bos\.system\b',          'os.system: can execute arbitrary OS commands'),
    (r'\bos\.popen\b',           'os.popen: can execute arbitrary OS commands'),
    (r'\beval\s*\(',             'eval(): executes arbitrary Python code'),
    (r'\bexec\s*\(',             'exec(): executes arbitrary Python code'),
    (r'__import__\s*\(',         '__import__(): dynamic import of arbitrary modules'),
    (r'\bshutil\.rmtree\b',      'shutil.rmtree: recursive directory deletion'),
    (r'\bos\.remove\b',          'os.remove: file deletion'),
    (r'\bopen\s*\([^)]*["\']w', 'open(..., "w"): writes to arbitrary file paths'),
    (r'\bsocket\b',              'socket: network access'),
    (r'\brequests\b',            'requests: HTTP/network access'),
    (r'\burllib\b',              'urllib: HTTP/network access'),
    (r'\bpickle\b',              'pickle: can execute code on deserialisation'),
    (r'\bctypes\b',              'ctypes: low-level OS/memory access'),
]


def _scan_controller_code(code: str) -> list:
    """Return a list of human-readable warning strings for any dangerous
    patterns found in *code*.  Empty list means the code looks clean."""
    import re
    warnings = []
    for pattern, reason in _CTRL_DANGEROUS_PATTERNS:
        if re.search(pattern, code):
            warnings.append(reason)
    # Syntax check
    try:
        compile(code, '<controller>', 'exec')
    except SyntaxError as exc:
        warnings.insert(0, f'SyntaxError: {exc}')
    return warnings


@require_POST
def update_sim_controller(request, sim_id):
    """POST: set controller FK (and legacy controller_file) for a Simulation."""
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    from .models import Controller
    sim = get_object_or_404(Simulation, pk=sim_id)
    fname = request.POST.get('controller_file', '').strip()
    if fname and not fname.endswith('.py'):
        return JsonResponse({'error': 'Controller file must be a .py file.'}, status=400)
    # Update legacy field
    sim.controller_file = fname
    # Update FK: find matching Controller record if one exists
    if fname:
        ctrl = Controller.objects.filter(file=fname).first()
        sim.controller = ctrl  # may be None if not yet registered
    else:
        sim.controller = None
    sim.save(update_fields=['controller_file', 'controller'])
    ctrl_id = sim.controller_id
    return JsonResponse({'controller_file': sim.controller_file, 'controller_id': ctrl_id})


@require_POST
def save_controller_file(request):
    """POST: scan, then save a controller .py file.  Returns any warnings."""
    from django.http import JsonResponse
    from r2h2.config import get_controllers_dir
    fname = request.POST.get('filename', '').strip()
    code  = request.POST.get('code', '')
    if not fname:
        return JsonResponse({'error': 'filename is required.'}, status=400)
    if not fname.endswith('.py'):
        fname += '.py'
    # Enforce naming: lowercase letters/digits/underscores, starting with a letter
    import re as _re
    stem = fname[:-3]  # strip .py
    if not _re.match(r'^[a-z][a-z0-9_]*$', stem):
        return JsonResponse(
            {'error': 'Name must start with a letter and contain only lowercase letters, digits and underscores.'},
            status=400)
    # Block path traversal
    if '/' in fname or '\\' in fname or '..' in fname:
        return JsonResponse({'error': 'Invalid filename.'}, status=400)
    # Block empty files
    if not code.strip():
        return JsonResponse({'error': 'Controller file is empty.'}, status=400)
    # Static analysis — collect warnings but do not block (desktop-app policy)
    scan_warnings = _scan_controller_code(code)
    # Block on syntax errors (first item prefixed 'SyntaxError:') before writing
    if scan_warnings and scan_warnings[0].startswith('SyntaxError'):
        return JsonResponse({'error': scan_warnings[0]}, status=400)
    ctrl_dir = get_controllers_dir()
    # Block duplicate creation (allow overwrite only when the file already exists — i.e. editing)
    is_new_file = not (ctrl_dir / fname).exists()
    if is_new_file and fname == 'default_controller.py':
        return JsonResponse({'error': 'Cannot overwrite the built-in default controller.'}, status=403)
    (ctrl_dir / fname).write_text(code, encoding='utf-8')
    # Create or update the Controller DB record
    from .models import Controller
    import datetime
    author = request.POST.get('author', '').strip()
    ctrl, created = Controller.objects.get_or_create(
        file=fname,
        defaults={
            'name':         fname.replace('_', ' ').replace('.py', ''),
            'author':       author,
            'date_created': datetime.date.today(),
            'verified':     False,
        },
    )
    # Append an edit-history entry
    history = ctrl.edit_history if isinstance(ctrl.edit_history, list) else []
    history.append({
        'datetime': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'author':   author or (ctrl.author or 'unknown'),
    })
    update_fields = ['edit_history']
    if not created and author:
        ctrl.author = author
        update_fields.append('author')
    ctrl.edit_history = history
    ctrl.save(update_fields=update_fields)
    return JsonResponse({
        'saved':         fname,
        'files':         _list_controller_files(),
        'warnings':      scan_warnings,
        'controller_id': ctrl.pk,
    })


def get_controller_file(request):
    """GET: return source code of a controller file."""
    from django.http import JsonResponse, Http404
    from r2h2.config import get_controllers_dir
    fname = request.GET.get('filename', '').strip()
    if not fname or not fname.endswith('.py') or '/' in fname or '\\' in fname or '..' in fname:
        return JsonResponse({'error': 'Invalid filename.'}, status=400)
    ctrl_dir = get_controllers_dir()
    path = ctrl_dir / fname
    if not path.exists():
        # For default_controller.py, attempt to seed it now from the package
        # template (handles Windows installs where seeding was skipped).
        if fname == 'default_controller.py':
            from pathlib import Path as _Path
            template_src = _Path(__file__).resolve().parent.parent / 'r2h2' / 'defaults' / 'controller_template.py'
            if template_src.exists():
                import shutil as _shutil
                _shutil.copy(template_src, path)
        if not path.exists():
            raise Http404
    return JsonResponse({'filename': fname, 'code': path.read_text(encoding='utf-8')})


@require_POST
def rename_controller_file(request):
    """POST: rename a controller .py file and update the Controller DB record."""
    from django.http import JsonResponse
    from r2h2.config import get_controllers_dir
    from .models import Controller
    old_name = request.POST.get('old_filename', '').strip()
    new_name = request.POST.get('new_filename', '').strip()
    if not old_name or not new_name:
        return JsonResponse({'error': 'Both old_filename and new_filename are required.'}, status=400)
    for n in (old_name, new_name):
        if not n.endswith('.py') or '/' in n or '\\' in n or '..' in n:
            return JsonResponse({'error': f'Invalid filename: {n}'}, status=400)
    if old_name == 'default_controller.py':
        return JsonResponse({'error': 'The default controller cannot be renamed.'}, status=403)
    if new_name == 'default_controller.py':
        return JsonResponse({'error': 'Cannot rename to default_controller.py.'}, status=400)
    ctrl_dir = get_controllers_dir()
    old_path = ctrl_dir / old_name
    new_path = ctrl_dir / new_name
    if not old_path.exists():
        return JsonResponse({'error': f'{old_name} not found.'}, status=404)
    if new_path.exists():
        return JsonResponse({'error': f'{new_name} already exists.'}, status=400)
    old_path.rename(new_path)
    # Update DB record
    ctrl = Controller.objects.filter(file=old_name).first()
    if ctrl:
        ctrl.file = new_name
        ctrl.save(update_fields=['file'])
    # Update any Simulations that referenced the old filename
    from .models import Simulation
    Simulation.objects.filter(controller_file=old_name).update(controller_file=new_name)
    return JsonResponse({
        'renamed': True,
        'old':     old_name,
        'new':     new_name,
        'files':   _list_controller_files(),
    })


def home(request):
    _component_models = [
        ('Battery',           Battery),
        ('ElectroCellPEM',    ElectroCellPEM),
        ('ElectrolyserUnit',  ElectrolyserUnit),
        ('ThermalProperties', ThermalProperties),
    ]
    components = []
    for table_name, model_cls in _component_models:
        mi = getattr(model_cls, 'MetaInfo', None)
        label = getattr(mi, 'verbose_name_plural',
                getattr(mi, 'verbose_name', table_name.replace('_', ' ')))
        components.append({
            'table': table_name,
            'label': label,
            'count': model_cls.objects.count(),
        })
    return render(request, 'dashboard/home.html', {'components': components})



### -------------------------------- Browse -------------------------------- ###


def format_cell_value(value):
    if value is None or value == '':
        return '—'
    s = str(value)
    m = re.match(r'^([\[\(])(.*)([\]\)])$', s, re.DOTALL)
    if m:
        bracket_open, inner, bracket_close = m.group(1), m.group(2), m.group(3)
        parts = [p.strip() for p in inner.split(',')]
        if len(parts) >= 3:
            def fmt(p):
                try:
                    return f"{float(p):.6g}"
                except ValueError:
                    return p
            full_array = re.escape(s)
            count = len(parts)
            preview = f"{bracket_open}{fmt(parts[0])}, ..., {fmt(parts[-1])}{bracket_close}"
            return mark_safe(
                f'{preview}<br>'
                f'<span class="array-badge" data-tooltip="{full_array}" '
                f'style="white-space:nowrap;cursor:help;background:#e0e0e0;'
                f'border-radius:999px;padding:1px 8px;font-size:0.75em;">'
                f'array [{count}]</span>'
            )
    return s


# Field types that are skipped in the add-new modal (output/array/auto fields)
_SKIP_FIELD_TYPES = ()
_SKIP_JSON = True   # hide JSONField arrays from the modal


def _modal_fields_for(model_class):
    """Return a list of form-field dicts for the Add New / Edit modals.

    If MetaInfo.ui_display_fields is defined, only fields listed there are
    included and their human-readable labels are used, in the defined order.
    Fields named 'id' and auto-set fields are always excluded.
    """
    from django.db import models as dm

    # Build a lookup of all concrete fields by name for fast access
    field_map = {
        f.name: f
        for f in model_class._meta.get_fields()
        if hasattr(f, 'column')
    }

    # Determine which fields to include and their labels
    metainfo = getattr(model_class, 'MetaInfo', None)
    ui_display_fields = getattr(metainfo, 'ui_display_fields', None)

    if ui_display_fields:
        # Use only the fields listed in ui_display_fields, in order, skipping 'id'
        candidate_items = [
            (fname, label)
            for fname, label in ui_display_fields.items()
            if fname != 'id' and fname in field_map
        ]
    else:
        # Fall back: all concrete fields except 'id'
        candidate_items = [
            (f.name, f.name)
            for f in model_class._meta.get_fields()
            if hasattr(f, 'column') and f.name != 'id'
        ]

    fields = []
    for fname, label in candidate_items:
        f = field_map[fname]

        # Skip auto-set fields
        if getattr(f, 'auto_now_add', False) or getattr(f, 'auto_now', False):
            continue
        # Skip JSONField arrays (runtime/output data)
        if isinstance(f, dm.JSONField):
            continue

        # Determine input type
        if isinstance(f, dm.BooleanField):
            ftype = 'checkbox'
        elif isinstance(f, (dm.IntegerField, dm.PositiveIntegerField,
                            dm.FloatField, dm.DecimalField)):
            ftype = 'number'
        elif isinstance(f, dm.TextField):
            ftype = 'textarea'
        elif hasattr(f, 'choices') and f.choices:
            ftype = 'select'
        else:
            ftype = 'text'

        # Resolve default value
        default = None
        if f.default is not dm.fields.NOT_PROVIDED:
            default = f.default() if callable(f.default) else f.default

        fields.append({
            'name': fname,
            'label': label,
            'ftype': ftype,
            'default': default,
            'choices': list(f.choices) if (hasattr(f, 'choices') and f.choices) else [],
            'help_text': getattr(f, 'help_text', '') or '',
            'null': getattr(f, 'null', False),
            'group': '',
        })

    # Annotate each field with its editable_groups membership
    editable_groups = getattr(metainfo, 'editable_groups', None)
    if editable_groups:
        field_to_group = {}
        for gname, fnames in editable_groups.items():
            for fname in fnames:
                field_to_group[fname] = gname
        for f in fields:
            f['group'] = field_to_group.get(f['name'], '')

    return fields


def add_component(request, table_name):
    """POST: create a new component from submitted form data and redirect to browse."""
    from django.shortcuts import redirect
    from django.db import models as dm
    import re as _re
    if request.method == 'POST':
        model_class = apps.get_model('dashboard', table_name)
        kwargs = {}
        for f in model_class._meta.get_fields():
            if not hasattr(f, 'column'):
                continue
            if f.name == 'id':
                continue
            if getattr(f, 'auto_now_add', False) or getattr(f, 'auto_now', False):
                continue
            if isinstance(f, dm.JSONField):
                continue
            raw = request.POST.get(f.name)
            if raw is None:
                continue
            if isinstance(f, dm.BooleanField):
                kwargs[f.name] = (raw.lower() in ('on', 'true', '1', 'yes'))
            elif isinstance(f, (dm.FloatField, dm.DecimalField)):
                try:
                    kwargs[f.name] = float(raw.replace(',', ''))
                except ValueError:
                    pass
            elif isinstance(f, (dm.IntegerField, dm.PositiveIntegerField)):
                try:
                    kwargs[f.name] = int(raw.replace(',', ''))
                except ValueError:
                    pass
            else:
                kwargs[f.name] = raw

        # Make the name unique: strip any existing suffix, find next integer
        if 'name' in kwargs and hasattr(model_class, '_meta'):
            base_name = _re.sub(r'-\d+$', '', kwargs['name'])
            existing = set(
                model_class.objects
                .filter(name__regex=rf'^{_re.escape(base_name)}(-\d+)?$')
                .values_list('name', flat=True)
            )
            if base_name in existing or existing:
                n = 1
                while f'{base_name}-{n}' in existing:
                    n += 1
                kwargs['name'] = f'{base_name}-{n}'

        model_class.objects.create(**kwargs)
    return redirect('dashboard-browse', table_name=table_name)


def get_component(request, table_name, pk):
    """GET: return JSON of all editable field values for a single component record,
    plus metadata about linked simulations."""
    from django.http import JsonResponse
    from django.db import models as dm
    model_class = apps.get_model('dashboard', table_name)
    try:
        obj = model_class.objects.get(pk=pk)
    except model_class.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)
    data = {}
    for f in model_class._meta.get_fields():
        if not hasattr(f, 'column'):
            continue
        if getattr(f, 'auto_now_add', False) or getattr(f, 'auto_now', False):
            continue
        if isinstance(f, dm.JSONField):
            continue
        val = getattr(obj, f.name, None)
        data[f.name] = val if val is not None else ''
    # Attach linked simulation info via reverse M2M accessor
    linked_sims = []
    if hasattr(obj, 'simulation_set'):
        for sim in obj.simulation_set.all().order_by('name'):
            linked_sims.append({'id': sim.pk, 'name': sim.name})
    data['_linked_sims'] = linked_sims
    return JsonResponse(data)


@require_POST
def edit_component(request, table_name, pk):
    """POST: update an existing component record from submitted form data.
    Supports mode=direct (default) and mode=copy.
    In copy mode: clones the object, links the copy to the selected sims
    (copy_sim_ids), and removes original from those sims.
    """
    from django.http import JsonResponse
    from django.db import models as dm
    from dashboard.models import Simulation
    model_class = apps.get_model('dashboard', table_name)
    try:
        obj = model_class.objects.get(pk=pk)
    except model_class.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)

    mode = request.POST.get('_mode', 'direct')
    validation_errors = []

    def _apply_fields(target):
        from datetime import date as _date
        for f in model_class._meta.get_fields():
            if not hasattr(f, 'column'):
                continue
            if f.name == 'id':
                continue
            if getattr(f, 'auto_now_add', False) or getattr(f, 'auto_now', False):
                continue
            if isinstance(f, dm.JSONField):
                continue
            raw = request.POST.get(f.name)
            if raw is None:
                continue
            if isinstance(f, dm.BooleanField):
                setattr(target, f.name, raw.lower() in ('on', 'true', '1', 'yes'))
            elif isinstance(f, dm.DateField) and not isinstance(f, dm.DateTimeField):
                raw_date = raw.strip()
                if raw_date == '':
                    if f.null:
                        setattr(target, f.name, None)
                    else:
                        validation_errors.append(f'{f.name}: this field is required.')
                else:
                    try:
                        setattr(target, f.name, _date.fromisoformat(raw_date))
                    except ValueError:
                        validation_errors.append(
                            f'{f.name}: invalid calendar date (expected YYYY-MM-DD).'
                        )
            elif isinstance(f, (dm.FloatField, dm.DecimalField)):
                try:
                    setattr(target, f.name, float(raw.replace(',', '')))
                except ValueError:
                    pass
            elif isinstance(f, (dm.IntegerField, dm.PositiveIntegerField)):
                try:
                    setattr(target, f.name, int(raw.replace(',', '')))
                except ValueError:
                    pass
            else:
                setattr(target, f.name, raw)

    if mode == 'copy':
        # Determine which sim IDs should get the copy
        raw_ids = request.POST.getlist('copy_sim_ids')
        try:
            copy_sim_ids = [int(i) for i in raw_ids]
        except (ValueError, TypeError):
            return JsonResponse({'error': 'Invalid sim IDs'}, status=400)
        # Find the M2M field name on Simulation that points to this model
        m2m_field_name = None
        for rel in model_class._meta.get_fields():
            if rel.many_to_many and not rel.concrete:
                m2m_field_name = rel.field.name  # e.g. 'batteries'
                break
        if m2m_field_name is None:
            return JsonResponse({'error': 'No M2M relation found'}, status=400)
        # Clone the object
        copy_obj = model_class.objects.get(pk=pk)
        copy_obj.pk = None
        _apply_fields(copy_obj)
        if validation_errors:
            return JsonResponse({'error': '; '.join(validation_errors)}, status=400)
        # Ensure the name is unique (exclude original pk)
        if hasattr(copy_obj, 'name'):
            base = copy_obj.name
            candidate = base
            counter = 2
            while model_class.objects.filter(name=candidate).exists():
                candidate = f"{base} ({counter})"
                counter += 1
            copy_obj.name = candidate
        copy_obj.save()
        # Re-link: for each selected sim, swap original → copy
        for sim in Simulation.objects.filter(pk__in=copy_sim_ids):
            m2m_mgr = getattr(sim, m2m_field_name)
            m2m_mgr.remove(obj)
            m2m_mgr.add(copy_obj)
        return JsonResponse({'ok': True, 'pk': copy_obj.pk, 'mode': 'copy'})
    else:
        _apply_fields(obj)
        if validation_errors:
            return JsonResponse({'error': '; '.join(validation_errors)}, status=400)
        # Ensure the name is unique (allow the object to keep its own current name)
        if hasattr(obj, 'name'):
            base = obj.name
            candidate = base
            counter = 2
            while model_class.objects.filter(name=candidate).exclude(pk=pk).exists():
                candidate = f"{base} ({counter})"
                counter += 1
            obj.name = candidate
        obj.save()
        return JsonResponse({'ok': True, 'pk': pk, 'mode': 'direct'})


@require_POST
def delete_component(request, table_name, pk):
    """POST: delete a single component record."""
    from django.http import JsonResponse
    model_class = apps.get_model('dashboard', table_name)
    try:
        obj = model_class.objects.get(pk=pk)
    except model_class.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)
    obj.delete()
    return JsonResponse({'ok': True})


def browse(request, table_name=None):

    model_class = apps.get_model('dashboard', table_name)

    # Use ui_column_names or ui_display_fields from MetaInfo if available,
    # else fall back to all model fields
    metainfo = getattr(model_class, 'MetaInfo', None)
    ui_column_names = (
        getattr(metainfo, 'ui_column_names', None)
        or getattr(metainfo, 'ui_display_fields', None)
    )
    ui_nice_name = getattr(metainfo, 'verbose_name_plural',
                   getattr(metainfo, 'ui_nice_name', table_name.replace('_', ' ')))
    ui_verbose_name = getattr(metainfo, 'verbose_name', ui_nice_name)

    if ui_column_names:
        columns = list(ui_column_names.keys())    # field names for data lookup
        headers = list(ui_column_names.values())  # display labels for table headers
    else:
        columns = [field.name for field in model_class._meta.fields]
        headers = columns  # fall back: use field name as header

    # Identify which of the requested columns are FK fields, so we can
    # select_related them and resolve their display value via str() rather
    # than showing a raw integer ID.
    fk_fields = {
        f.name
        for f in model_class._meta.get_fields()
        if hasattr(f, 'many_to_one') and f.many_to_one and f.name in columns
    }

    qs = model_class.objects.order_by('-id')
    if fk_fields:
        qs = qs.select_related(*fk_fields)

    _modal_fields = _modal_fields_for(model_class)

    # Build grouped structure for modals
    _metainfo = getattr(model_class, 'MetaInfo', None)
    _editable_groups = getattr(_metainfo, 'editable_groups', None)
    if _editable_groups:
        _field_to_group = {}
        for _gname, _fnames in _editable_groups.items():
            for _fname in _fnames:
                _field_to_group[_fname] = _gname
        _grouped = {gname: [] for gname in _editable_groups}
        _ungrouped = []
        for _f in _modal_fields:
            _g = _f.get('group', '')
            if _g and _g in _grouped:
                _grouped[_g].append(_f)
            else:
                _ungrouped.append(_f)
        modal_fields_grouped = []
        for _gname in _editable_groups:
            if _grouped[_gname]:
                modal_fields_grouped.append({'name': _gname, 'fields': _grouped[_gname]})
    else:
        modal_fields_grouped = [{'name': '', 'fields': _modal_fields}]

    # ── Rebuild columns/rows using editable_groups for the main table ─────
    _ui_labels = getattr(_metainfo, 'ui_display_fields', {})
    _all_field_map = {f.name: f for f in model_class._meta.get_fields() if hasattr(f, 'column')}

    if _editable_groups:
        # Build group_col_fields: gname → [(fname, label), ...]
        _group_col_fields = {}
        _all_grouped_fnames = set()
        for _gname, _fnames in _editable_groups.items():
            _group_col_fields[_gname] = []
            for _fname in _fnames:
                if _fname in _all_field_map:
                    _label = _ui_labels.get(_fname, _fname)
                    _group_col_fields[_gname].append((_fname, _label))
                    _all_grouped_fnames.add(_fname)

        # Columns: one per group only (name shown via edit/delete buttons, not as a column)
        columns = []
        headers = []
        for _gname in _editable_groups:
            columns.append(_gname)
            headers.append(_gname.replace('_', ' '))
        group_column_names = set(_editable_groups.keys())

        rows = []
        for obj in qs:
            row = {'row_pk': obj.pk}
            if 'name' in _all_field_map:
                row['name'] = format_cell_value(getattr(obj, 'name', ''))
            for _gname, _field_pairs in _group_col_fields.items():
                _badges = []
                for _fname, _flabel in _field_pairs:
                    _val = getattr(obj, _fname, None)
                    if _val is not None and _val != '':
                        try:
                            _fval = f'{float(_val):.4g}'
                        except (TypeError, ValueError):
                            _fval = str(_val)
                        _help = getattr(_all_field_map.get(_fname), 'help_text', '') or ''
                        _title = f'{_flabel}: {_help}' if _help else _flabel
                        _badges.append(
                            f'<span class="param-badge" title="{_title}">'
                            f'<span class="param-badge-name">{_flabel}</span>'
                            f'<span class="param-badge-val">{_fval}</span>'
                            f'</span>'
                        )
                row[_gname] = mark_safe(''.join(_badges)) if _badges else '—'
            linked_sims = []
            if hasattr(obj, 'simulation_set'):
                linked_sims = list(obj.simulation_set.values_list('name', flat=True).order_by('name'))
            row['sim_count'] = len(linked_sims)
            row['sim_names'] = ', '.join(linked_sims)
            rows.append(row)
    else:
        group_column_names = set()
        rows = []
        for obj in qs:
            row = {'row_pk': obj.pk}
            for col in columns:
                val = getattr(obj, col, '')
                if col in fk_fields:
                    related = val
                    row[col] = str(related) if related is not None else '—'
                else:
                    row[col] = format_cell_value(val)
            linked_sims = []
            if hasattr(obj, 'simulation_set'):
                linked_sims = list(obj.simulation_set.values_list('name', flat=True).order_by('name'))
            row['sim_count'] = len(linked_sims)
            row['sim_names'] = ', '.join(linked_sims)
            rows.append(row)

    context = {
        'columns': columns,
        'headers': headers,
        'rows': rows,
        'group_column_names': list(group_column_names),
        'total_count': model_class.objects.count(),
        'ui_nice_name': ui_nice_name,
        'ui_verbose_name': ui_verbose_name,
        'table_name': table_name,
        'modal_fields': [f for f in _modal_fields if f.get('group')] if _editable_groups else _modal_fields,
        'modal_fields_json': json.dumps([f for f in _modal_fields if f.get('group')] if _editable_groups else _modal_fields),
        'modal_fields_grouped': modal_fields_grouped,
        'modal_fields_grouped_json': json.dumps(modal_fields_grouped),
    }

    return render(request, "dashboard/browse.html", context)


# ─── Wind data upload ──────────────────────────────────────────────────────────

import os as _os
from django.http import JsonResponse
from r2h2.config import get_wind_data_dir, load_config, get_config_path
import yaml as _yaml
import pandas as _pd
from pathlib import Path as _Path


def wind_preview_data(request, sim_id):
    """GET: return hourly wind speed and per-turbine mean power JSON
    for the wind HDF5 file linked to this simulation.

    Response shape:
        { hours: [int, ...],
          wind_speed: [float, ...],
          turbines: [{turbine: int, power: [float, ...]}, ...],
          n_turbines: int,
          n_hours: int,
          filename: str }
    """
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    import h5py, numpy as np

    MAX_PTS = 2000          # max points to return per series (downsample above this)

    sim = get_object_or_404(Simulation, pk=sim_id)
    try:
        wind_path = _resolve_wind_h5_path(sim)
    except ValueError as e:
        return JsonResponse({'error': str(e)}, status=404)

    try:
        with h5py.File(wind_path, 'r') as f:
            # ── Wind speed ──────────────────────────────────────────────────
            if '/WindSpeed' not in f:
                return JsonResponse({'error': 'No /WindSpeed dataset found in wind file.'}, status=404)
            ws = f['/WindSpeed'][:].ravel()           # shape → (N_hours,)
            n_hours = int(len(ws))
            step = max(1, n_hours // MAX_PTS)
            hours      = list(range(0, n_hours, step))
            wind_speed = [None if np.isnan(v) else float(v) for v in ws[::step]]

            # ── Per-turbine hourly mean power ────────────────────────────────
            turbines = []
            if '/TurbPowerInput' in f:
                # shape: (N_hours, N_turbines, N_timesteps)
                tp = f['/TurbPowerInput'][:]
                mean_hourly = tp.mean(axis=2)          # → (N_hours, N_turbines)
                n_turbs = mean_hourly.shape[1]
                for t in range(n_turbs):
                    # Replace NaN with None so JSON serialises to null (NaN is invalid JSON)
                    raw = mean_hourly[::step, t]
                    power = [None if np.isnan(v) else float(v) for v in raw]
                    turbines.append({
                        'turbine': t + 1,
                        'power':   power,
                    })

        return JsonResponse({
            'hours':      hours,
            'wind_speed': wind_speed,
            'turbines':   turbines,
            'n_turbines': len(turbines),
            'n_hours':    n_hours,
            'filename':   wind_path.name,
        })
    except Exception as e:
        return JsonResponse({'error': f'Could not read wind file: {e}'}, status=500)


def wind_data(request):
    """Page listing uploaded wind HDF5 files with a drag-and-drop uploader."""
    from django.conf import settings as django_settings
    wind_dir = get_wind_data_dir()
    media_root = _Path(django_settings.MEDIA_ROOT)

    # Fetch all linked WindInput records keyed by filename
    wi_qs = WindInput.objects.exclude(wind_file='').exclude(wind_file__isnull=True)
    wi_by_name = {_Path(wi.wind_file.name).name: wi for wi in wi_qs}

    # Backfill metadata for records that were uploaded before the new fields existed,
    # and re-introspect any that used the old comma separator (no '|' in h5_datasets).
    needs_save = []
    for wi in wi_by_name.values():
        needs_reintrospect = (
            (wi.h5_datasets == '' and wi.ts_start is None)
            or (wi.h5_datasets and '|' not in wi.h5_datasets)
        )
        if needs_reintrospect:
            full_path = media_root / wi.wind_file.name
            if full_path.exists():
                meta = _introspect_wind_h5(full_path)
                for k, v in meta.items():
                    setattr(wi, k, v)
                needs_save.append(wi)
    if needs_save:
        fields = ['h5_datasets', 'ts_start', 'ts_end', 'ts_resolution', 'ts_n_hours']
        for wi in needs_save:
            wi.save(update_fields=fields)

    def _file_meta(f):
        wi = wi_by_name.get(f.name)
        if wi is not None:
            meta = {
                'h5_datasets': wi.h5_datasets or '',
                'ts_n_hours':  wi.ts_n_hours,
                'ts_resolution': wi.ts_resolution,
                'ts_start': wi.ts_start,
                'ts_end':   wi.ts_end,
            }
        else:
            # Introspect unlinked files on the fly (read-only, not saved)
            meta = _introspect_wind_h5(f)
        return {
            'name': f.name,
            'size_mb': round(f.stat().st_size / 1e6, 2),
            'modified': _pd.Timestamp(f.stat().st_mtime, unit='s').strftime('%Y-%m-%d %H:%M'),
            'wind_input': wi,
            **meta,
        }

    files = sorted(
        [_file_meta(f) for f in wind_dir.iterdir()
         if f.suffix.lower() in ('.h5', '.hdf5', '.hdf')],
        key=lambda x: x['name'],
    )
    cfg = load_config() or {}
    wind_dir_str = cfg.get('paths', {}).get('wind_data_dir', str(wind_dir))
    return render(request, 'dashboard/wind_data.html', {
        'files': files,
        'wind_dir': wind_dir_str,
    })


def _introspect_wind_h5(path):
    """Return a dict of HDF5 metadata for a wind file.

    Keys: h5_datasets, ts_start, ts_end, ts_resolution, ts_n_hours.
    All values default to safe empty/None on any error.
    """
    meta = {'h5_datasets': '', 'ts_start': None, 'ts_end': None,
            'ts_resolution': None, 'ts_n_hours': None}
    try:
        import h5py, numpy as np
        datasets = []
        with h5py.File(path, 'r') as f:
            def _collect(name, obj):
                if isinstance(obj, h5py.Dataset):
                    datasets.append(f'/{name} {list(obj.shape)} {obj.dtype}')
            f.visititems(_collect)
            meta['h5_datasets'] = '|'.join(datasets)
            # Time axis
            if '/Time' in f:
                t = f['/Time'][:].ravel()
                if len(t) > 1:
                    meta['ts_start']      = float(t[0])
                    meta['ts_end']        = float(t[-1])
                    meta['ts_resolution'] = float(round(t[1] - t[0], 6))
            # Hour count from WindSpeed
            if '/WindSpeed' in f:
                ws = f['/WindSpeed'][:].ravel()
                meta['ts_n_hours'] = int(len(ws))
    except Exception:
        pass
    return meta


@require_POST
def wind_data_upload(request):
    """AJAX endpoint: receive one or more HDF5 files and save them to wind_data_dir."""
    if not request.FILES:
        return JsonResponse({'error': 'No files received.'}, status=400)

    wind_dir = get_wind_data_dir()
    saved = []
    errors = []

    for field_name, uploaded_file in request.FILES.items():
        name = _Path(uploaded_file.name).name  # strip any path components
        if _Path(name).suffix.lower() not in ('.h5', '.hdf5', '.hdf'):
            errors.append(f'{name}: not a recognised HDF5 extension (.h5 / .hdf5 / .hdf)')
            continue
        dest = wind_dir / name
        try:
            with open(dest, 'wb') as fh:
                for chunk in uploaded_file.chunks():
                    fh.write(chunk)
            # Introspect HDF5 metadata
            meta = _introspect_wind_h5(dest)
            # Create or retrieve the linked WindInput record and update metadata
            rel_path = 'wind_data/' + name
            wind_input, created = WindInput.objects.get_or_create(
                wind_file=rel_path,
                defaults={'name': _Path(name).stem, **meta},
            )
            if not created:
                for k, v in meta.items():
                    setattr(wind_input, k, v)
                wind_input.save(update_fields=list(meta.keys()))
            saved.append({
                'name': name,
                'size_mb': round(dest.stat().st_size / 1e6, 2),
                'wind_input_id': wind_input.id,
            })
        except OSError as exc:
            errors.append(f'{name}: {exc}')

    status = 200 if saved else 400
    return JsonResponse({'saved': saved, 'errors': errors}, status=status)


@require_POST
def wind_data_set_dir(request):
    """AJAX endpoint: update wind_data_dir in config.yaml."""
    new_dir = request.POST.get('wind_data_dir', '').strip()
    if not new_dir:
        return JsonResponse({'error': 'No directory provided.'}, status=400)
    try:
        from r2h2.config import update_wind_data_dir
        cfg = update_wind_data_dir(new_dir)
        return JsonResponse({'wind_data_dir': cfg['paths']['wind_data_dir']})
    except Exception as exc:
        return JsonResponse({'error': str(exc)}, status=500)
