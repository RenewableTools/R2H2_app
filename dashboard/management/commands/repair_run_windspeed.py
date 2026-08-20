from __future__ import annotations

import datetime as dt
from pathlib import Path

import h5py
import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand

from dashboard.models import SimulationRun


def _resolve_wind_paths(sim) -> list[Path]:
    """Return linked wind HDF5 paths in sequence order."""
    entries = (
        sim.wind_input_entries
        .select_related('wind_input')
        .exclude(wind_input__wind_file='')
        .exclude(wind_input__wind_file__isnull=True)
        .order_by('sequence')
    )
    out: list[Path] = []
    for entry in entries:
        p = Path(settings.MEDIA_ROOT) / entry.wind_input.wind_file.name
        if p.exists():
            out.append(p)
    return out


def _load_full_windspeed(paths: list[Path]) -> np.ndarray:
    """Load and concatenate /WindSpeed from all linked wind files."""
    if not paths:
        return np.array([], dtype=float)
    chunks = []
    for p in paths:
        with h5py.File(p, 'r') as f:
            if '/WindSpeed' not in f:
                continue
            arr = np.asarray(f['/WindSpeed'][:], dtype=float).reshape(-1)
            if arr.size:
                chunks.append(arr)
    if not chunks:
        return np.array([], dtype=float)
    return np.concatenate(chunks, axis=0)


def _run_hour_window(run: SimulationRun, total_hours: int) -> tuple[int, int]:
    """Compute [start, end) hour window used by a run."""
    sim = run.simulation
    if run.run_start_date:
        datum = sim.datum_date or dt.date(run.run_start_date.year, 1, 1)
        start = max(0, int((run.run_start_date - datum).days * 24))
    else:
        start = 0

    if run.run_end_date:
        datum_for_end = sim.datum_date or dt.date(run.run_end_date.year, 1, 1)
        end = int((run.run_end_date - datum_for_end).days * 24) + 24
    else:
        end = total_hours

    start = min(start, total_hours)
    end = min(max(end, start), total_hours)
    return start, end


def _year_lengths(h5_path: Path) -> list[int]:
    """Read per-year hour lengths from existing run output."""
    lengths: list[tuple[int, int]] = []
    with h5py.File(h5_path, 'r') as f:
        year_keys = sorted(
            [k for k in f.keys() if k.startswith('year_')],
            key=lambda s: int(s.split('_')[1]),
        )
        for yk in year_keys:
            idx = int(yk.split('_')[1])
            grp = f[yk]
            n = None
            for gname, dname in (
                ('battery', 'arSoc'),
                ('h2', 'arTotalH2'),
                ('electrolyser', 'arElecOnAv'),
            ):
                if gname in grp and dname in grp[gname]:
                    n = int(np.asarray(grp[gname][dname][:]).shape[0])
                    break
            if n is None:
                pgrp = grp.get('power')
                if pgrp is not None and 'arWindPowerFilt' in pgrp:
                    n = int(np.asarray(pgrp['arWindPowerFilt'][:]).shape[0])
            if n is None:
                n = 0
            lengths.append((idx, n))
    return [n for _, n in sorted(lengths, key=lambda t: t[0])]


def _rewrite_windspeed_datasets(h5_path: Path, per_year: list[np.ndarray]) -> tuple[int, int]:
    """Overwrite /year_*/power/arWindSpeed datasets; return (updated, created)."""
    updated = 0
    created = 0
    with h5py.File(h5_path, 'r+') as f:
        year_keys = sorted(
            [k for k in f.keys() if k.startswith('year_')],
            key=lambda s: int(s.split('_')[1]),
        )
        for i, yk in enumerate(year_keys):
            if i >= len(per_year):
                break
            yr = f[yk]
            pwr = yr.require_group('power')
            if 'arWindSpeed' in pwr:
                del pwr['arWindSpeed']
                updated += 1
            else:
                created += 1
            pwr.create_dataset(
                'arWindSpeed',
                data=np.asarray(per_year[i], dtype=np.float64),
                compression='gzip',
                compression_opts=4,
            )
    return updated, created


class Command(BaseCommand):
    help = 'Repair wind-speed series in existing SimulationRun HDF5 output files.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--run-id',
            type=int,
            default=None,
            help='Repair only one SimulationRun id.',
        )
        parser.add_argument(
            '--sim-id',
            type=int,
            default=None,
            help='Repair runs only for one Simulation id.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Preview changes without writing files.',
        )

    def handle(self, *args, **options):
        run_id = options['run_id']
        sim_id = options['sim_id']
        dry_run = options['dry_run']

        qs = SimulationRun.objects.select_related('simulation').exclude(output_path='')
        if run_id is not None:
            qs = qs.filter(pk=run_id)
        if sim_id is not None:
            qs = qs.filter(simulation_id=sim_id)

        runs = list(qs.order_by('pk'))
        if not runs:
            self.stdout.write(self.style.WARNING('No runs matched the selection.'))
            return

        self.stdout.write(
            f"Scanning {len(runs)} run output file(s){' (dry-run)' if dry_run else ''}..."
        )

        fixed = 0
        skipped = 0
        failed = 0

        for run in runs:
            abs_path = Path(settings.MEDIA_ROOT) / run.output_path
            if not abs_path.exists():
                self.stdout.write(
                    self.style.WARNING(f"run {run.pk}: output missing at {abs_path} (skip)")
                )
                skipped += 1
                continue

            try:
                wind_paths = _resolve_wind_paths(run.simulation)
                ws_full = _load_full_windspeed(wind_paths)
                if ws_full.size == 0:
                    self.stdout.write(
                        self.style.WARNING(f"run {run.pk}: no linked WindSpeed source found (skip)")
                    )
                    skipped += 1
                    continue

                start, end = _run_hour_window(run, int(ws_full.size))
                ws = ws_full[start:end]

                lengths = _year_lengths(abs_path)
                if not lengths:
                    self.stdout.write(self.style.WARNING(f"run {run.pk}: no year_* groups (skip)"))
                    skipped += 1
                    continue

                per_year = []
                offset = 0
                for n in lengths:
                    seg = ws[offset: offset + n]
                    if seg.size < n:
                        # Preserve expected length; leave unknown tail as NaN.
                        pad = np.full(n - seg.size, np.nan, dtype=float)
                        seg = np.concatenate([seg, pad])
                    per_year.append(seg)
                    offset += n

                if dry_run:
                    self.stdout.write(
                        f"run {run.pk}: would write arWindSpeed for {len(per_year)} year group(s) "
                        f"from hours [{start}:{end})"
                    )
                    fixed += 1
                    continue

                updated, created = _rewrite_windspeed_datasets(abs_path, per_year)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"run {run.pk}: repaired ({len(per_year)} years, updated={updated}, created={created}, hours=[{start}:{end}))"
                    )
                )
                fixed += 1
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"run {run.pk}: failed ({exc})"))
                failed += 1

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry-run complete. candidates={fixed}, skipped={skipped}, failed={failed}"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Repair complete. fixed={fixed}, skipped={skipped}, failed={failed}"
                )
            )