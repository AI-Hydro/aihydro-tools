"""
Forcing Data Module

This module provides tools for extracting, processing, and formatting hydrometeorological
forcing data for hydrological modeling, following CAMELS Part 2 methodology.

Key capabilities:
- Fetch basin-averaged climate forcing from GridMET
- Calculate multiple PET methods (Hargreaves, Penman-Monteith)
- Export forcing in model-ready formats
- Generate CAMELS-compatible time series

References:
    - Newman et al. (2015) - CAMELS forcing data
    - Hargreaves & Samani (1985) - Reference evapotranspiration
    - Allen et al. (1998) - FAO-56 Penman-Monteith
"""

from __future__ import annotations

import warnings
from typing import Tuple, Dict, Optional, Union
from pathlib import Path
import numpy as np
import pandas as pd

try:
    import xarray as xr
    import pygridmet as gridmet
    _DEPS_AVAILABLE = True
except ImportError:
    _DEPS_AVAILABLE = False

warnings.filterwarnings('ignore')


# ---------------------------------------------------------------------------
# Bounded, chunked, cancellable fetching (T1-R)
#
# A 35-year daily request used to be one blocking call per variable with no budget: it outlived the
# client's timeout, ignored a cancel and held a worker for as long as the upstream took.  The
# window is now fetched in ``AIHYDRO_FORCING_CHUNK_YEARS``-year chunks (default 5).  Between
# chunks the call checks for a cancel and its wall-clock budget; every upstream call is bounded by
# ``AIHYDRO_FETCH_CHUNK_TIMEOUT_S`` (default 300 s); progress is reported per chunk.  The product
# that served the first chunk is pinned for the rest, so the series is one product's, exactly as a
# single call would return; if a pinned chunk cannot be served, the variable is fetched as one
# range, as before.  aihydro-data's disk cache makes a re-run after a timeout resume cheaply.
# ---------------------------------------------------------------------------
_DEFAULT_CHUNK_YEARS = 5
_DEFAULT_CHUNK_TIMEOUT_S = 300.0
_DEFAULT_BUDGET_S = 480.0       # under the client's ~600 s timeout: stop with a retryable answer, not a hang


def _env_float(name: str, default: float) -> float:
    import os

    try:
        v = float(os.environ.get(name, default))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _chunk_windows(start_date: str, end_date: str, years: int) -> list[tuple[str, str]]:
    """Inclusive ``(start, end)`` ISO windows of ``years`` calendar years covering the range."""
    start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
    if years < 1 or end < start:
        return [(start_date, end_date)]
    out: list[tuple[str, str]] = []
    cur = start
    while cur <= end:
        nxt = cur + pd.DateOffset(years=years)
        stop = min(nxt - pd.Timedelta(days=1), end)
        out.append((cur.strftime("%Y-%m-%d"), stop.strftime("%Y-%m-%d")))
        cur = nxt
    return out


def _merge_chunk_provenance(parts: list) -> dict:
    """Per-variable coverage facts of a chunked fetch, as one call over the whole window would report.

    Head comes from the first chunk, tail from the last, interior chunk edges count as internal gaps;
    ranges are concatenated (a gap that spans a chunk boundary is reported as two adjacent ranges).
    """
    g = lambda r, k: getattr(r, k, None)          # noqa: E731
    internal = None
    if all(g(r, "days_missing_internal") is not None for r in parts):
        internal = sum(g(r, "days_missing_internal") for r in parts)
        internal += sum((g(r, "days_missing_head") or 0) for r in parts[1:])
        internal += sum((g(r, "days_missing_tail") or 0) for r in parts[:-1])
    flags = [g(r, "coverage_complete") for r in parts]
    notes: list = []
    for r in parts:
        for note in (g(r, "notes") or []):
            if note not in notes:
                notes.append(note)
    ranges: list = []
    for r in parts:
        ranges += list(g(r, "missing_ranges") or [])
    return {
        "units": g(parts[0], "units"),
        "coverage_complete": None if all(f is None for f in flags) else all(bool(f) for f in flags if f is not None),
        "days_missing_head": g(parts[0], "days_missing_head"),
        "days_missing_tail": g(parts[-1], "days_missing_tail"),
        "days_missing_internal": internal,
        "missing_ranges": ranges,
        "notes": notes,
    }


def _fetch_variable_chunked(adata_fetch, adata_var, geom, start_date, end_date, fetch_kwargs):
    """``aihydro_data.fetch`` for one variable, chunked, bounded and cancellable.

    Returns an object with ``.data`` (DataFrame with a ``date`` column), ``.product``, ``.source``.
    """
    from types import SimpleNamespace

    from ai_hydro.core.cancel import (
        ToolCancelled, ToolDeadlineExceeded, check_cancelled_or_expired, chunk_done,
        report_progress, run_bounded,
    )

    timeout = _env_float("AIHYDRO_FETCH_CHUNK_TIMEOUT_S", _DEFAULT_CHUNK_TIMEOUT_S)
    years = int(_env_float("AIHYDRO_FORCING_CHUNK_YEARS", _DEFAULT_CHUNK_YEARS))
    windows = _chunk_windows(start_date, end_date, years)

    def one(s, e, kwargs):
        return run_bounded(adata_fetch, adata_var, geom, s, e, timeout=timeout,
                           label=f"fetch {adata_var} {s}..{e}", **kwargs)

    check_cancelled_or_expired()
    if len(windows) == 1:
        res1 = one(start_date, end_date, fetch_kwargs)
        chunk_done()
        return res1

    frames, parts, first = [], [], None
    for i, (s, e) in enumerate(windows):
        check_cancelled_or_expired()
        report_progress(i, len(windows), f"{adata_var} {s}..{e}")
        kwargs = dict(fetch_kwargs)
        if first is not None and "product" not in kwargs:
            kwargs["mode"], kwargs["product"] = "manual", first.product
        try:
            res = one(s, e, kwargs)
        except (ToolCancelled, ToolDeadlineExceeded):
            raise
        except Exception:
            if first is None:
                raise
            # A pinned chunk the product cannot serve: answer exactly as a single call would.
            return one(start_date, end_date, fetch_kwargs)
        first = first or res
        parts.append(res)
        frames.append(res.data)
        chunk_done()
    report_progress(len(windows), len(windows), adata_var)
    df = pd.concat(frames, ignore_index=True)
    if "date" in df.columns:
        df = df.drop_duplicates(subset="date", keep="first").sort_values("date").reset_index(drop=True)
    return SimpleNamespace(data=df, product=first.product, source=first.source, **_merge_chunk_provenance(parts))


def fetch_forcing_data(
    watershed_geom,
    start_date: str,
    end_date: str,
    variables: Optional[list] = None
) -> pd.DataFrame:
    """
    Fetch basin-averaged daily forcing data from GridMET.
    
    Parameters
    ----------
    watershed_geom : shapely.geometry
        Watershed boundary geometry
    start_date : str
        Start date in YYYY-MM-DD format
    end_date : str
        End date in YYYY-MM-DD format
    variables : list, optional
        List of variables to fetch. Default: all available
        
    Returns
    -------
    pd.DataFrame
        Daily forcing data with columns for each variable
        
    Examples
    --------
    >>> from ai_hydro.tools.forcing import fetch_forcing_data
    >>> df = fetch_forcing_data(
    ...     watershed_geom,
    ...     '2010-01-01',
    ...     '2020-12-31'
    ... )
    >>> print(df.head())
    """
    if not _DEPS_AVAILABLE:
        raise ImportError("forcing data requires: pip install aihydro-tools[data]")
    try:
        print(f"Fetching GridMET forcing data from {start_date} to {end_date}...")
        
        # Default variables: all GridMET climate variables
        if variables is None:
            variables = ["pr", "tmmn", "tmmx", "srad", "vs", "sph", "pet"]
        
        # Fetch data
        ds = gridmet.get_bygeom(
            geometry=watershed_geom,
            dates=(start_date, end_date),
            variables=variables,
            crs="EPSG:4326"
        )
        
        print(f"✓ Retrieved {len(ds.time)} days of forcing data")
        
        # Calculate spatial mean (basin average)
        df_list = []
        for var in variables:
            if var in ds.data_vars:
                var_mean = ds[var].mean(dim=["lat", "lon"])
                series = var_mean.to_series()
                series.index = pd.to_datetime(series.index).tz_localize(None)
                df_list.append(series.rename(var))
        
        # Combine all variables
        df = pd.concat(df_list, axis=1)
        
        # Unit conversions to standard format
        if 'tmmn' in df.columns:
            df['tmmn'] = df['tmmn'] - 273.15  # K to °C
        if 'tmmx' in df.columns:
            df['tmmx'] = df['tmmx'] - 273.15  # K to °C
        if 'tmmn' in df.columns and 'tmmx' in df.columns:
            df['tavg'] = (df['tmmn'] + df['tmmx']) / 2  # Average temperature
        
        # Rename to CAMELS-style column names
        column_mapping = {
            'pr': 'prcp_mm',
            'tmmn': 'tmin_C',
            'tmmx': 'tmax_C',
            'tavg': 'tavg_C',
            'srad': 'srad_Wm2',
            'vs': 'wind_ms',
            'sph': 'sph_kgkg',
            'pet': 'pet_mm'
        }
        df = df.rename(columns={k: v for k, v in column_mapping.items() if k in df.columns})
        
        df.index.name = 'date'
        df = df.reset_index()
        
        print(f"✓ Processed forcing data: {len(df)} days, {len(df.columns)-1} variables")
        return df
        
    except Exception as e:
        print(f"✗ Error fetching forcing data: {str(e)}")
        return None


def calculate_pet_hargreaves(
    df: pd.DataFrame,
    latitude: float
) -> pd.DataFrame:
    """
    Calculate PET using Hargreaves-Samani method.
    
    Parameters
    ----------
    df : pd.DataFrame
        DataFrame with tmin_C, tmax_C columns
    latitude : float
        Watershed centroid latitude in decimal degrees
        
    Returns
    -------
    pd.DataFrame
        DataFrame with added pet_hargreaves_mm column
        
    References
    ----------
    Hargreaves & Samani (1985) - Reference crop evapotranspiration
    """
    try:
        print("Calculating Hargreaves-Samani PET...")
        
        # Require temperature columns
        if 'tmin_C' not in df.columns or 'tmax_C' not in df.columns:
            print("  Warning: Missing temperature columns, skipping Hargreaves PET")
            return df
        
        tavg = (df['tmin_C'] + df['tmax_C']) / 2
        tdiff = np.sqrt(np.maximum(df['tmax_C'] - df['tmin_C'], 0))
        
        # Extraterrestrial radiation approximation
        # If solar radiation available, use it; otherwise estimate
        if 'srad_Wm2' in df.columns:
            ra_approx = df['srad_Wm2'] * 0.0864 * 1.5  # W/m² to MJ/m²/day
        else:
            # Day of year for seasonal variation
            doy = pd.to_datetime(df['date']).dt.dayofyear
            # Simplified Ra estimation
            ra_approx = 15.0 + 10.0 * np.sin(2 * np.pi * (doy - 81) / 365)
        
        # Hargreaves-Samani equation
        # PET = 0.0023 * Ra * (Tavg + 17.8) * sqrt(Tmax - Tmin)
        df['pet_hargreaves_mm'] = 0.0023 * ra_approx * (tavg + 17.8) * tdiff
        
        # Ensure non-negative
        df['pet_hargreaves_mm'] = np.maximum(df['pet_hargreaves_mm'], 0)
        
        print(f"✓ Hargreaves PET calculated (mean: {df['pet_hargreaves_mm'].mean():.2f} mm/day)")
        return df
        
    except Exception as e:
        print(f"✗ Error calculating Hargreaves PET: {str(e)}")
        return df


def calculate_water_balance_metrics(df: pd.DataFrame) -> Dict[str, float]:
    """
    Calculate water balance statistics from forcing data.
    
    Parameters
    ----------
    df : pd.DataFrame
        Forcing data with prcp_mm and pet_mm columns
        
    Returns
    -------
    dict
        Water balance metrics
    """
    try:
        stats = {}
        
        # Require precipitation and PET
        if 'prcp_mm' not in df.columns or 'pet_mm' not in df.columns:
            print("  Warning: Missing prcp_mm or pet_mm, cannot compute water balance")
            return stats
        
        # Annual totals on the monthly-climatology basis (a missing day is not a dry day, and a missing season
        # does not re-weight the year); None + a reason when a calendar month is not usable.
        dates = _dates_of(df)
        stats['mean_annual_precip_mm'], p_why = _mean_annual_total(dates, df['prcp_mm'])
        stats['mean_annual_pet_mm'], e_why = _mean_annual_total(dates, df['pet_mm'])
        if p_why:
            stats['mean_annual_precip_reason'] = p_why
        if e_why:
            stats['mean_annual_pet_reason'] = e_why

        # Aridity index, from those same two totals
        if (stats['mean_annual_precip_mm'] or 0) > 0 and stats['mean_annual_pet_mm'] is not None:
            stats['aridity_index'] = stats['mean_annual_pet_mm'] / stats['mean_annual_precip_mm']
        else:
            stats['aridity_index'] = None

        # Temperature statistics
        if 'tavg_C' in df.columns:
            stats['mean_annual_temp_C'], t_why = _mean_annual_temp(dates, df['tavg_C'])
            if t_why:
                stats['mean_annual_temp_reason'] = t_why

        # Precipitation characteristics, over the days that have a value
        # Wet-day indicator (1 mm threshold), NaN where precipitation is NaN, as a ratio per calendar month on the
        # same monthly basis (WMO-No. 1203 s4.4.1(d)); None + reason when a month is not usable.
        wet = (df['prcp_mm'] > 1.0).astype(float).where(df['prcp_mm'].notna())
        clim, w_why = _monthly_climatology(dates, wet)
        if clim is None:
            stats['wet_day_frequency'] = None
            stats['wet_days_per_year'] = None
            stats['wet_day_reason'] = w_why
        else:
            stats['wet_day_frequency'] = float(sum(clim[m] * _MONTH_DAYS[m - 1] for m in range(1, 13)) / 365.25)
            stats['wet_days_per_year'] = stats['wet_day_frequency'] * 365.25

        # Snow fraction (approximate: precip when temp <= 0°C)
        if 'tavg_C' in df.columns:
            snow_days = (df['tavg_C'] <= 0) & (df['prcp_mm'] > 0)
            stats['snow_fraction'] = df.loc[snow_days, 'prcp_mm'].sum() / df['prcp_mm'].sum()
        
        print(f"✓ Water balance metrics calculated")
        return stats
        
    except Exception as e:
        print(f"✗ Error calculating water balance: {str(e)}")
        return {}


def export_forcing_data(
    df: pd.DataFrame,
    gauge_id: str,
    output_dir: Union[str, Path],
    formats: list = ['csv', 'netcdf']
) -> Dict[str, str]:
    """
    Export forcing data in multiple formats for model compatibility.
    
    Parameters
    ----------
    df : pd.DataFrame
        Forcing data
    gauge_id : str
        Gauge identifier
    output_dir : str or Path
        Output directory
    formats : list
        List of formats to export: 'csv', 'netcdf', 'summa', 'vic'
        
    Returns
    -------
    dict
        Dictionary of {format: filepath} for exported files
    """
    try:
        print(f"Exporting forcing data in {len(formats)} format(s)...")
        
        output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True, parents=True)
        
        exported_files = {}
        
        # CSV export
        if 'csv' in formats:
            csv_path = output_dir / f"{gauge_id}_forcing.csv"
            df.to_csv(csv_path, index=False)
            exported_files['csv'] = str(csv_path)
            print(f"  ✓ CSV: {csv_path}")
        
        # NetCDF export
        if 'netcdf' in formats:
            nc_path = output_dir / f"{gauge_id}_forcing.nc"
            ds = df.set_index('date').to_xarray()
            ds.to_netcdf(nc_path)
            exported_files['netcdf'] = str(nc_path)
            print(f"  ✓ NetCDF: {nc_path}")
        
        # SUMMA format (specialized format for SUMMA model)
        if 'summa' in formats:
            summa_path = output_dir / f"{gauge_id}_forcing_summa.txt"
            _export_summa_format(df, summa_path)
            exported_files['summa'] = str(summa_path)
            print(f"  ✓ SUMMA: {summa_path}")
        
        # VIC format
        if 'vic' in formats:
            vic_path = output_dir / f"{gauge_id}_forcing_vic.txt"
            _export_vic_format(df, vic_path)
            exported_files['vic'] = str(vic_path)
            print(f"  ✓ VIC: {vic_path}")
        
        print(f"✓ Forcing data exported successfully")
        return exported_files
        
    except Exception as e:
        print(f"✗ Error exporting forcing data: {str(e)}")
        return {}


def _export_summa_format(df: pd.DataFrame, filepath: Path):
    """Export forcing data in SUMMA model format"""
    # SUMMA expects: year, month, day, hour, prcp, temp, etc.
    df_copy = df.copy()
    df_copy['date'] = pd.to_datetime(df_copy['date'])
    df_copy['year'] = df_copy['date'].dt.year
    df_copy['month'] = df_copy['date'].dt.month
    df_copy['day'] = df_copy['date'].dt.day
    df_copy['hour'] = 12  # Assume daily data at noon
    
    cols = ['year', 'month', 'day', 'hour']
    if 'prcp_mm' in df_copy.columns:
        cols.append('prcp_mm')
    if 'tavg_C' in df_copy.columns:
        cols.append('tavg_C')
    if 'srad_Wm2' in df_copy.columns:
        cols.append('srad_Wm2')
    if 'wind_ms' in df_copy.columns:
        cols.append('wind_ms')
    
    df_copy[cols].to_csv(filepath, sep='\t', index=False, float_format='%.3f')


def _export_vic_format(df: pd.DataFrame, filepath: Path):
    """Export forcing data in VIC model format"""
    # VIC expects: prcp, tmax, tmin, wind
    df_copy = df.copy()
    
    cols = []
    if 'prcp_mm' in df_copy.columns:
        cols.append('prcp_mm')
    if 'tmax_C' in df_copy.columns:
        cols.append('tmax_C')
    if 'tmin_C' in df_copy.columns:
        cols.append('tmin_C')
    if 'wind_ms' in df_copy.columns:
        cols.append('wind_ms')
    
    df_copy[cols].to_csv(filepath, sep='\t', index=False, header=False, float_format='%.3f')


def create_forcing_dataset(
    watershed_geom,
    gauge_id: str,
    start_date: str,
    end_date: str,
    output_dir: Union[str, Path],
    latitude: Optional[float] = None,
    export_formats: list = ['csv']
) -> Tuple[pd.DataFrame, Dict[str, float], Dict[str, str]]:
    """
    Complete workflow: fetch, process, and export forcing data.
    
    Parameters
    ----------
    watershed_geom : shapely.geometry
        Watershed boundary
    gauge_id : str
        Gauge identifier
    start_date : str
        Start date YYYY-MM-DD
    end_date : str
        End date YYYY-MM-DD
    output_dir : str or Path
        Output directory
    latitude : float, optional
        Watershed latitude for PET calculation
    export_formats : list
        Export formats (default: ['csv'])
        
    Returns
    -------
    df : pd.DataFrame
        Forcing data
    stats : dict
        Water balance statistics
    files : dict
        Exported file paths
        
    Examples
    --------
    >>> from ai_hydro.tools.forcing import create_forcing_dataset
    >>> df, stats, files = create_forcing_dataset(
    ...     watershed_geom,
    ...     '03335000',
    ...     '2010-01-01',
    ...     '2020-12-31',
    ...     './forcing_output'
    ... )
    """
    try:
        print(f"\n{'='*60}")
        print(f"Creating forcing dataset for gauge {gauge_id}")
        print(f"Period: {start_date} to {end_date}")
        print(f"{'='*60}\n")
        
        # Step 1: Fetch forcing data
        df = fetch_forcing_data(watershed_geom, start_date, end_date)
        if df is None:
            raise ValueError("Failed to fetch forcing data")
        
        # Step 2: Calculate additional PET if latitude provided
        if latitude is not None:
            df = calculate_pet_hargreaves(df, latitude)
        
        # Step 3: Calculate water balance metrics
        stats = calculate_water_balance_metrics(df)
        
        # Step 4: Export data
        files = export_forcing_data(df, gauge_id, output_dir, export_formats)
        
        print(f"\n{'='*60}")
        print("Forcing dataset creation complete!")
        print(f"{'='*60}\n")
        
        return df, stats, files
        
    except Exception as e:
        print(f"✗ Error creating forcing dataset: {str(e)}")
        return None, {}, {}


def generate_forcing_summary(
    df: pd.DataFrame,
    stats: Dict[str, float],
    gauge_id: str
) -> str:
    """
    Generate human-readable summary of forcing data.
    
    Parameters
    ----------
    df : pd.DataFrame
        Forcing data
    stats : dict
        Water balance statistics
    gauge_id : str
        Gauge identifier
        
    Returns
    -------
    str
        Formatted summary text
    """
    summary = "\n" + "="*60 + "\n"
    summary += f"FORCING DATA SUMMARY - Gauge {gauge_id}\n"
    summary += "="*60 + "\n\n"
    
    summary += f"Time Period: {df['date'].iloc[0]} to {df['date'].iloc[-1]}\n"
    summary += f"Total Days: {len(df)}\n"
    summary += f"Variables: {', '.join([c for c in df.columns if c != 'date'])}\n\n"
    
    summary += "WATER BALANCE:\n"
    if stats.get('mean_annual_precip_mm') is not None:
        summary += f"  Mean Annual Precipitation: {stats['mean_annual_precip_mm']:.1f} mm/year\n"
    if stats.get('mean_annual_pet_mm') is not None:
        summary += f"  Mean Annual PET: {stats['mean_annual_pet_mm']:.1f} mm/year\n"
    if stats.get('aridity_index') is not None:
        summary += f"  Aridity Index: {stats['aridity_index']:.2f}\n"
    if stats.get('mean_annual_temp_C') is not None:
        summary += f"  Mean Annual Temperature: {stats['mean_annual_temp_C']:.1f} °C\n\n"
    
    summary += "PRECIPITATION CHARACTERISTICS:\n"
    if stats.get('wet_days_per_year') is not None:
        summary += f"  Wet Days per Year: {stats['wet_days_per_year']:.0f}\n"
    if 'snow_fraction' in stats:
        summary += f"  Snow Fraction: {stats['snow_fraction']:.2f}\n\n"
    
    summary += "="*60 + "\n"

    return summary


#: Mean days of each calendar month (February 28.25): twelve of them sum to 365.25.
_MONTH_DAYS = (31.0, 28.25, 31.0, 30.0, 31.0, 30.0, 31.0, 31.0, 30.0, 31.0, 30.0, 31.0)


#: A calendar month enters the annual basis only if at least this fraction of the years in the record have a VALID
#: monthly value in it. WMO-No. 1203 s4.4.2 gives 80% for climate NORMALS; it is applied here by analogy (a record
#: of a few years is not a normal), so treat it as a guard on top of the sourced validity rule below.
_MIN_YEARS_WITH_DATA_PER_MONTH = 0.80

#: A year-month is a valid monthly MEAN when it has at most this many missing days and fewer than
#: ``_MAX_CONSECUTIVE_MISSING`` of them in a row: the sourced definition for a mean parameter, WMO-No. 1203 (2017)
#: s4.4.1(a) (10 or fewer missing days; run test as implemented here: longest missing run < 5 days).
_MAX_MISSING_DAYS_PER_MONTH = 10
_MAX_CONSECUTIVE_MISSING = 5


def _valid_monthly_means(series: "pd.Series") -> "pd.Series":
    """Mean of the finite days of every year-month that is a valid monthly mean (WMO-No. 1203 s4.4.1(a)).

    Indexed by (year, month). The calendar days of the month are the denominator of the missing count, so a
    record that starts or stops inside a month leaves that month short and, usually, invalid.
    """
    out = {}
    for (yr, mo), g in series.groupby([series.index.year, series.index.month]):
        n_days = pd.Period(year=int(yr), month=int(mo), freq="M").days_in_month
        present = pd.Series(False, index=pd.date_range(f"{yr}-{mo:02d}-01", periods=n_days, freq="D"))
        fin = g[np.isfinite(g)]
        present.loc[fin.index.normalize().intersection(present.index)] = True
        missing = ~present.values
        if missing.sum() > _MAX_MISSING_DAYS_PER_MONTH:
            continue
        run = longest = 0
        for m_ in missing:
            run = run + 1 if m_ else 0
            longest = max(longest, run)
        if longest >= _MAX_CONSECUTIVE_MISSING:
            continue
        if len(fin):
            out[(int(yr), int(mo))] = float(fin.mean())
    return pd.Series(out, dtype=float)


def _monthly_climatology(dates, series) -> tuple:
    """``(monthly mean per calendar month as a Series indexed 1..12 | None, reason | None)``.

    The estimator is a MONTHLY MEAN: each year-month that is valid (WMO-No. 1203 s4.4.1(a): at most 10 missing
    days, and no run of 5 or more) contributes the mean of its finite days; a calendar month's value is the mean of
    those monthly means. A calendar month is usable when at least 80% of the years in which it occurs in the record
    have a valid monthly value (WMO-No. 1203 s4.4.2, by analogy to normals; see ``_MIN_YEARS_WITH_DATA_PER_MONTH``).
    """
    d = pd.DatetimeIndex(pd.to_datetime(list(dates)))
    v = pd.to_numeric(pd.Series(np.asarray(series), index=d), errors="coerce")
    if len(d) == 0:
        return None, "empty record"
    if not np.isfinite(v).any():
        return None, "no finite day"
    span = pd.date_range(d.min().normalize(), d.max().normalize(), freq="D")
    occurs = pd.Series(1, index=pd.MultiIndex.from_arrays([span.year, span.month])).groupby(level=[0, 1]).sum()
    n_years = occurs.groupby(level=1).size()                       # years in which each month occurs in the span
    monthly = _valid_monthly_means(v)
    have = monthly.groupby(level=1).size() if len(monthly) else pd.Series(dtype=int)
    bad = []
    for m in range(1, 13):
        n_occ = int(n_years.get(m, 0))
        n_have = int(have.get(m, 0))
        if n_occ == 0 or n_have == 0 or n_have < _MIN_YEARS_WITH_DATA_PER_MONTH * n_occ - 1e-9:
            bad.append(m)
    if bad:
        return None, (f"calendar month(s) {bad} lack a valid monthly mean (at most 10 missing days and no run of 5 or "
                      f"more, WMO-No. 1203 s4.4.1(a)) in at least {_MIN_YEARS_WITH_DATA_PER_MONTH:.0%} of the years of "
                      "the record (80%: s4.4.2 for normals, applied by analogy)")
    return monthly.groupby(level=1).mean(), None


def _mean_annual_total(dates, series) -> tuple:
    """Mean annual total of a daily flux (mm/day -> mm/yr): ``(value | None, reason | None)``.

    Sum over the 12 calendar months of (mean of the finite days in that month) x (days in the month). A
    missing (NaN) day is neither a zero-flux day (the old ``sum / record length`` read 543 mm for 3 mm/day with
    half the days missing) nor, as a plain mean over the finite days would be, a license to over-weight the
    observed season: with a missing dry season the plain mean read +48%, with a missing wet season -48% in the
    methods review's test. The monthly climatology weights each calendar month by its length whatever the gaps
    are. ``(None, reason)`` when a calendar month is not usable (see :func:`_monthly_climatology`).
    """
    clim, why = _monthly_climatology(dates, series)
    if clim is None:
        return None, why
    return float(sum(clim[m] * _MONTH_DAYS[m - 1] for m in range(1, 13))), None


def _mean_annual_temp(dates, series) -> tuple:
    """Mean annual temperature on the same monthly basis: ``(value | None, reason | None)``."""
    clim, why = _monthly_climatology(dates, series)
    if clim is None:
        return None, why
    return float(sum(clim[m] * _MONTH_DAYS[m - 1] for m in range(1, 13)) / 365.25), None


def _dates_of(df: "pd.DataFrame"):
    return df["date"] if "date" in df.columns else df.index


def fetch_forcing_data_result(
    watershed_geojson: dict,
    start_date: str,
    end_date: str,
    gauge_id: Optional[str] = None,
    variables: Optional[list] = None,
) -> "HydroResult":
    """
    Fetch basin-averaged GridMET forcing data, returning a standardized HydroResult.

    Parameters
    ----------
    watershed_geojson : dict
        GeoJSON geometry dict (e.g. from delineate_watershed result).
    start_date : str
        Start date in YYYY-MM-DD format.
    end_date : str
        End date in YYYY-MM-DD format.
    gauge_id : str, optional
        Gauge ID for provenance metadata.
    variables : list, optional
        GridMET variable names to fetch. Default: all available.

    Returns
    -------
    HydroResult
        data keys: dates (ISO string list), plus one list per variable,
        plus water-balance summary stats (mean_annual_precip_mm, aridity_index, etc.)
    """
    from shapely.geometry import shape
    from ai_hydro.core import DataSource, HydroMeta, HydroResult, ToolError
    import numpy as np

    _TOOL_PATH = "ai_hydro.tools.forcing.fetch_forcing_data_result"
    _SOURCES = [
        DataSource(
            name="GridMET (Climatology Lab)",
            url="https://www.climatologylab.org/gridmet.html",
            citation=(
                "@article{abatzoglou2013development, "
                "title={Development of gridded surface meteorological data for "
                "ecological applications and modelling}, "
                "author={Abatzoglou, John T}, "
                "journal={International Journal of Climatology}, "
                "volume={33}, number={1}, pages={121--131}, year={2013}}"
            ),
        ),
    ]

    try:
        from ai_hydro import __version__

        watershed_geom = shape(watershed_geojson)
        df = fetch_forcing_data(
            watershed_geom=watershed_geom,
            start_date=start_date,
            end_date=end_date,
            variables=variables,
        )

        if df is None or df.empty:
            raise ToolError(
                code="NO_FORCING_DATA",
                message="fetch_forcing_data returned empty DataFrame",
                tool=_TOOL_PATH,
                recovery="Check date range and watershed geometry validity.",
            )

        # Build JSON-serializable dict
        clean: dict = {}

        # Dates as ISO strings
        if "date" in df.columns:
            clean["dates"] = [str(d) for d in df["date"]]
        else:
            clean["dates"] = [str(d) for d in df.index]

        # Each variable column as float list (NaN → None)
        var_cols = [c for c in df.columns if c != "date"]
        for col in var_cols:
            vals = df[col].tolist()
            clean[col] = [
                (float(v) if (v is not None and np.isfinite(float(v))) else None)
                for v in vals
            ]

        clean["n_days"] = len(df)
        clean["variables"] = var_cols

        # Summary stats
        pr_col = next((c for c in var_cols if c in ("pr", "precipitation")), None)
        pet_col = next((c for c in var_cols if c in ("pet", "potential_evapotranspiration")), None)
        tmmn_col = "tmmn" if "tmmn" in var_cols else None
        tmmx_col = "tmmx" if "tmmx" in var_cols else None

        if pr_col:
            annual_pr, why = _mean_annual_total(_dates_of(df), df[pr_col])
            clean["mean_annual_precip_mm"] = round(annual_pr, 2) if annual_pr is not None else None
            if why:
                clean["mean_annual_precip_reason"] = why
        if pet_col:
            annual_pet, why = _mean_annual_total(_dates_of(df), df[pet_col])
            clean["mean_annual_pet_mm"] = round(annual_pet, 2) if annual_pet is not None else None
            if why:
                clean["mean_annual_pet_reason"] = why
        if pr_col and pet_col and (clean.get("mean_annual_precip_mm") or 0) > 0 \
                and clean.get("mean_annual_pet_mm") is not None:
            clean["aridity_index"] = round(
                clean["mean_annual_pet_mm"] / clean["mean_annual_precip_mm"], 3
            )
        if tmmn_col and tmmx_col:
            mean_k, why = _mean_annual_temp(_dates_of(df), (df[tmmn_col] + df[tmmx_col]) / 2)
            clean["mean_annual_temp_C"] = round(mean_k - 273.15, 2) if mean_k is not None else None
            if why:
                clean["mean_annual_temp_reason"] = why

        return HydroResult(
            data=clean,
            meta=HydroMeta(
                tool=_TOOL_PATH,
                version=__version__,
                gauge_id=gauge_id,
                sources=_SOURCES,
                params={
                    "start_date": start_date,
                    "end_date": end_date,
                    "variables": variables or "all",
                },
            ),
        )

    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            code="FORCING_FETCH_FAILED",
            message=str(exc),
            tool=_TOOL_PATH,
            recovery=(
                "Ensure watershed_geojson is a valid GeoJSON polygon and "
                "dates are in YYYY-MM-DD format. "
                "Install forcing extras: pip install 'ai-hydro[forcing]'"
            ),
        ) from exc


# ---------------------------------------------------------------------------
# Routing table for fetch_forcing_data_routed
# ---------------------------------------------------------------------------

# Map aihydro-tools GridMET-style variable names → (aihydro_data variable, output column)
_ADATA_ROUTES: dict[str, tuple[str, str]] = {
    "pr":   ("precipitation", "prcp_mm"),
    "tmmx": ("tmax",          "tmax_C"),
    "tmmn": ("tmin",          "tmin_C"),
    "pet":  ("pet",           "pet_mm"),
}

# Dataset-family prefix → per-variable product IDs in aihydro_data
_PRODUCT_FAMILIES: dict[str, dict[str, str]] = {
    "GRIDMET": {
        "precipitation": "GRIDMET_PRECIP",
        "tmax":          "GRIDMET_TMAX",
        "tmin":          "GRIDMET_TMIN",
        "pet":           "GRIDMET_PET",
    },
    "ERA5L": {
        "precipitation": "ERA5L_PRECIP",
        "tmax":          "ERA5L_TMAX",
        "tmin":          "ERA5L_TMIN",
        "pet":           "ERA5L_PET",
    },
    "DAYMET": {
        "precipitation": "DAYMET_PRECIP",
        "tmax":          "DAYMET_TMAX",
        "tmin":          "DAYMET_TMIN",
    },
    "CHIRPS": {"precipitation": "CHIRPS"},
    "IMERG":  {"precipitation": "IMERG_PRECIP"},
}

# Variables with no aihydro_data equivalent (GridMET/CONUS-only)
_GRIDMET_ONLY_VARS: frozenset[str] = frozenset({"srad", "vs", "sph", "rmax", "rmin", "erc"})

# GridMET → output column name mapping for CONUS-only variables
_GRIDMET_ONLY_COL: dict[str, str] = {
    "srad": "srad_Wm2",
    "vs":   "wind_ms",
    "sph":  "sph_kgkg",
    "rmax": "rmax_pct",
    "rmin": "rmin_pct",
    "erc":  "erc",
}


def _with_forcing_budget(fn):
    """Bound the whole request by ``AIHYDRO_FORCING_BUDGET_S`` (default 480 s), however it is called."""
    import functools

    @functools.wraps(fn)
    def bounded(*args, **kwargs):
        from ai_hydro.core.cancel import ToolDeadlineExceeded, budget, chunk_counter

        secs = _env_float("AIHYDRO_FORCING_BUDGET_S", _DEFAULT_BUDGET_S)
        with chunk_counter() as counter, budget(secs):
            try:
                return fn(*args, **kwargs)
            except ToolDeadlineExceeded as exc:
                raise ToolDeadlineExceeded(
                    f"the forcing request used its {secs:g} s budget with {counter['done']} of "
                    f"{counter['total']} chunks fetched",
                    chunks_done=counter["done"], chunks_total=counter["total"],
                ) from exc

    return bounded


@_with_forcing_budget
def fetch_forcing_data_routed(
    watershed_geojson: dict,
    start_date: str,
    end_date: str,
    variables: Optional[list] = None,
    product: Optional[str] = None,
) -> "HydroResult":
    """
    Fetch basin-averaged forcing via aihydro_data router (globally robust).

    Core variables (pr → precipitation, tmmx → tmax, tmmn → tmin, pet) are
    fetched through ``aihydro_data.fetch()``, which auto-routes to the best
    available product for the geometry's region:

    - **CONUS**:         GridMET (4 km, daily)
    - **North America**: Daymet / ERA5-Land
    - **Global**:        CHIRPS (precipitation), ERA5-Land (temperature, PET)

    CONUS-specific GridMET variables (srad, vs, sph, rmax, rmin, erc) are
    fetched via pygridmet directly when the basin is inside CONUS, and silently
    omitted for global basins.

    Parameters
    ----------
    watershed_geojson : dict
        GeoJSON geometry dict.
    start_date, end_date : str
        Date range in YYYY-MM-DD format.
    variables : list | None
        GridMET-style variable names to request (e.g. ``["pr","tmmx","tmmn","pet"]``).
        Defaults to all four core routable variables.
    product : str | None
        Optional dataset-family pin:
        ``"GRIDMET"``, ``"ERA5L"``, ``"DAYMET"``, ``"CHIRPS"``, ``"IMERG"``.
        A full product ID (e.g. ``"ERA5L_PRECIP"``) pins only precipitation.
        ``None`` (default) → auto-routing for every variable.

    Returns
    -------
    HydroResult
        data keys: dates (ISO strings), one list per variable (output units),
        n_days, variables, mean_annual_precip_mm, aridity_index, product, source.
        ``product`` and ``source`` report the actual backend(s) that served the data.
    """
    from shapely.geometry import shape as _shapely_shape
    from ai_hydro.core import DataSource, HydroMeta, HydroResult, ToolError
    from ai_hydro.core.cancel import ToolCancelled, ToolDeadlineExceeded

    _TOOL_PATH = "ai_hydro.data.forcing.fetch_forcing_data_routed"

    try:
        from aihydro_data import fetch as _adata_fetch
    except ImportError as exc:
        raise ToolError(
            code="AIHYDRO_DATA_NOT_INSTALLED",
            message="aihydro-data is required for global-aware forcing fetch.",
            tool=_TOOL_PATH,
            recovery="pip install 'aihydro-tools[data]'",
        ) from exc

    geom = _shapely_shape(watershed_geojson)
    requested: list[str] = variables or list(_ADATA_ROUTES.keys())
    from ai_hydro.core.cancel import chunk_total_add
    chunk_total_add(len([v for v in requested if v in _ADATA_ROUTES]) * len(_chunk_windows(
        start_date, end_date, int(_env_float("AIHYDRO_FORCING_CHUNK_YEARS", _DEFAULT_CHUNK_YEARS)))))

    # ── Resolve product family → per-variable product IDs ──────────────────
    family_map: dict[str, str] = {}
    if product:
        # Try family prefix match first (GRIDMET, ERA5L, DAYMET, CHIRPS, IMERG)
        prod_upper = product.upper().split("_")[0]
        if prod_upper in _PRODUCT_FAMILIES:
            family_map = _PRODUCT_FAMILIES[prod_upper]
        else:
            # Treat as a literal product ID → apply to precipitation only
            family_map = {"precipitation": product}

    # ── Fetch routable variables via aihydro_data ───────────────────────────
    frames: list[pd.DataFrame] = []
    products_used: list[str] = []
    sources_used: list[str] = []
    warnings_list: list[str] = []
    variables_failed: list[dict] = []
    variable_provenance: dict[str, dict] = {}

    for var in requested:
        if var not in _ADATA_ROUTES:
            continue  # CONUS-only — handled below
        adata_var, out_col = _ADATA_ROUTES[var]

        pinned = family_map.get(adata_var)
        fetch_kwargs: dict = {"aggregation": "basin_mean"}
        if pinned:
            fetch_kwargs["mode"] = "manual"
            fetch_kwargs["product"] = pinned

        try:
            result = _fetch_variable_chunked(
                _adata_fetch, adata_var, geom, start_date, end_date, fetch_kwargs)
            df_var = result.data.copy()
            # Normalise: rename value column to our output name
            val_col = next((c for c in df_var.columns if c != "date"), None)
            if val_col is None:
                warnings_list.append(f"{var}: empty DataFrame returned")
                variables_failed.append({"variable": var, "output": out_col,
                                         "reason": "empty DataFrame returned"})
                continue
            df_var = df_var.rename(columns={val_col: out_col})
            df_var["date"] = pd.to_datetime(df_var["date"])

            # K → °C conversion for temperature variables
            if out_col in ("tmax_C", "tmin_C"):
                _convert_needed = False
                try:
                    from aihydro_data.products import get_product as _get_prod
                    spec = _get_prod(result.product)
                    _convert_needed = (getattr(spec, "units", None) == "K")
                except Exception:
                    # Heuristic fallback: GridMET/ERA5L return Kelvin (values > 200)
                    _convert_needed = bool(df_var[out_col].dropna().mean() > 200)
                if _convert_needed:
                    df_var[out_col] = df_var[out_col] - 273.15

            frames.append(df_var)
            products_used.append(result.product)
            sources_used.append(str(result.source))
            # Per-variable provenance: which product served it, in what units, and whether the
            # requested window was covered (the merged frame cannot tell variables apart).
            variable_provenance[out_col] = {
                "variable": adata_var,
                "product": result.product,
                "source": str(result.source),
                "units": getattr(result, "units", "") or None,
                "coverage_complete": getattr(result, "coverage_complete", None),
                "days_missing_head": getattr(result, "days_missing_head", None),
                "days_missing_tail": getattr(result, "days_missing_tail", None),
                "days_missing_internal": getattr(result, "days_missing_internal", None),
                "missing_ranges": list(getattr(result, "missing_ranges", []) or [])[:20],
                "notes": [str(n) for n in (getattr(result, "notes", []) or [])][:10],
            }
        except (ToolCancelled, ToolDeadlineExceeded):
            raise                       # stop the whole request: a cancel or an exhausted budget is not a warning
        except Exception as exc:
            warnings_list.append(f"{adata_var}: {exc}")
            variables_failed.append({"variable": var, "output": out_col,
                                     "product": pinned, "reason": str(exc)[:300]})

    # ── Supplement: CONUS-only GridMET vars (srad, vs, sph, …) ────────────
    conus_only_requested = [v for v in requested if v in _GRIDMET_ONLY_VARS]
    if conus_only_requested:
        try:
            from ai_hydro.analysis._dem import _geom_in_conus
            if _geom_in_conus(geom) and _DEPS_AVAILABLE:
                from ai_hydro.core.cancel import run_bounded as _run_bounded

                ds = _run_bounded(
                    gridmet.get_bygeom, geom, dates=(start_date, end_date),
                    variables=conus_only_requested,
                    timeout=_env_float("AIHYDRO_FETCH_CHUNK_TIMEOUT_S", _DEFAULT_CHUNK_TIMEOUT_S) * 2,
                    label="fetch gridmet-only variables",
                )
                for var in conus_only_requested:
                    if var in ds.data_vars:
                        series = ds[var].mean(dim=["lat", "lon"])
                        df_var = series.to_frame(name=_GRIDMET_ONLY_COL.get(var, var))
                        df_var.index = pd.to_datetime(df_var.index).tz_localize(None)
                        df_var = df_var.reset_index().rename(
                            columns={"index": "date", "time": "date"}
                        )
                        frames.append(df_var)
                products_used.append("GRIDMET_DIRECT")
            else:
                warnings_list.append(
                    f"CONUS-only variables skipped for non-CONUS basin: {conus_only_requested}"
                )
                variables_failed += [{"variable": v, "reason": "CONUS-only variable, basin outside CONUS"}
                                     for v in conus_only_requested]
        except (ToolCancelled, ToolDeadlineExceeded):
            raise
        except Exception as exc:
            warnings_list.append(f"CONUS-only fetch failed: {exc}")
            variables_failed += [{"variable": v, "reason": f"CONUS-only fetch failed: {exc}"[:300]}
                                 for v in conus_only_requested]

    # ── Guard ───────────────────────────────────────────────────────────────
    if not frames:
        raise ToolError(
            code="NO_FORCING_DATA",
            message=(
                "Could not fetch any forcing variables. "
                + ("; ".join(warnings_list) if warnings_list else "")
            ),
            tool=_TOOL_PATH,
            recovery=(
                "Ensure aihydro-data is installed: pip install 'aihydro-tools[data]'. "
                "For global basins, GEE auth may be required: aihydro-gee status. "
                "Try product='CHIRPS' for auth-free global precipitation."
            ),
        )

    # ── Merge all variable DataFrames on date ───────────────────────────────
    df = frames[0]
    for f in frames[1:]:
        df = df.merge(f, on="date", how="outer")
    df = df.sort_values("date").reset_index(drop=True)

    # Derived: mean temperature
    if "tmax_C" in df.columns and "tmin_C" in df.columns:
        df["tavg_C"] = (df["tmax_C"] + df["tmin_C"]) / 2

    # ── Build JSON-serialisable data dict ───────────────────────────────────
    clean: dict = {}
    clean["dates"] = [
        str(d.date()) if hasattr(d, "date") else str(d)
        for d in df["date"]
    ]
    var_cols = [c for c in df.columns if c != "date"]
    for col in var_cols:
        vals = df[col].tolist()
        clean[col] = [
            (float(v) if (v is not None and np.isfinite(float(v))) else None)
            for v in vals
        ]
    clean["n_days"] = len(df)
    clean["variables"] = var_cols
    clean["product"] = " | ".join(dict.fromkeys(products_used))
    clean["source"]  = " | ".join(dict.fromkeys(sources_used))
    if warnings_list:
        clean["_warnings"] = warnings_list
    # Asked-for versus served, by name. A variable that failed leaves a record, not a silent hole.
    clean["variables_requested"] = [v for v in requested]
    clean["variables_failed"] = variables_failed
    clean["variable_provenance"] = variable_provenance
    # Days with no value, per served variable column (a NaN cell is a missing value however the provider
    # reported its coverage).
    nan_cells = {c: int(df[c].isna().sum()) for c in df.columns
                 if c not in ("date", "tavg_C") and int(df[c].isna().sum()) > 0}
    clean["variables_with_missing_days"] = nan_cells
    clean["forcing_complete"] = bool(
        not variables_failed
        and not nan_cells
        and all(p.get("coverage_complete") is not False for p in variable_provenance.values())
    )

    # Summary stats - means over the days that have a value (a NaN day is not a zero day).
    for col, key in (("prcp_mm", "precip"), ("pet_mm", "pet")):
        if col in df.columns:
            n_fin = int(np.isfinite(pd.to_numeric(df[col], errors="coerce")).sum())
            clean[f"{key}_n_finite_days"] = n_fin
            clean[f"{key}_n_missing_days"] = int(len(df) - n_fin)
    for col, name in (("prcp_mm", "precip"), ("pet_mm", "pet")):
        if col in df.columns:
            v, why = _mean_annual_total(df["date"], df[col])
            clean[f"mean_annual_{name}_mm"] = round(v, 2) if v is not None else None
            if why:
                clean[f"mean_annual_{name}_reason"] = why
    # Aridity on the SAME basis for both: both are monthly-climatology totals, never one of those and one
    # sum over different day sets.
    if "prcp_mm" in df.columns and "pet_mm" in df.columns:
        if (clean.get("mean_annual_precip_mm") or 0) > 0 and clean.get("mean_annual_pet_mm") is not None:
            clean["aridity_index"] = round(clean["mean_annual_pet_mm"] / clean["mean_annual_precip_mm"], 3)
        else:
            clean["aridity_index"] = None
            clean["aridity_index_reason"] = (clean.get("mean_annual_precip_reason")
                                             or clean.get("mean_annual_pet_reason")
                                             or "precipitation total is not positive")
    if "tavg_C" in df.columns:
        v, why = _mean_annual_temp(df["date"], df["tavg_C"])
        clean["mean_annual_temp_C"] = round(v, 2) if v is not None else None
        if why:
            clean["mean_annual_temp_reason"] = why

    from ai_hydro import __version__
    return HydroResult(
        data=clean,
        meta=HydroMeta(
            tool=_TOOL_PATH,
            version=__version__,
            sources=[
                DataSource(name=p, url="")
                for p in dict.fromkeys(products_used)
            ],
            params={
                "start_date": start_date,
                "end_date":   end_date,
                "variables":  variables or "auto",
                "product":    product,
            },
        ),
    )


# ---------------------------------------------------------------------------
# @feature_tool kernel — the canonical, decorated entry point (Phase B)
# ---------------------------------------------------------------------------

def _make_forcing_kernel():
    """
    Build and return the @feature_tool-decorated forcing kernel.

    Deferred inside a factory so the aihydro_core import is lazy (avoids
    hard startup failure when aihydro-core is not installed).
    """
    try:
        from aihydro_core.features.compute import feature_tool as _feature_tool
    except ImportError:
        return None  # graceful degradation — MCP tool falls back to manual pattern

    @_feature_tool(product="forcing", citations=["gridmet", "chirps", "era5"])
    def _forcing_kernel(geom: dict, *, start_date: str, end_date: str,
                        variables=None, product=None) -> dict:
        """
        Pure forcing computation kernel.

        Called by the @feature_tool decorator after feature resolution and cache
        check. Receives the raw GeoJSON geometry dict and compute params; returns
        a result envelope ``{"data": {...}, "meta": {...}}``.

        The kernel is pure: no session I/O, no disk writes. Large daily arrays
        are stripped before storage; the MCP tool adds ``_data_file`` after the
        workspace write.

        Parameters
        ----------
        geom : dict   GeoJSON geometry resolved from the feature registry.
        start_date, end_date : str   YYYY-MM-DD
        variables : list | None      Subset of forcing vars (default: core 4)
        product : str | None         Dataset family pin (e.g. "ERA5L")
        """
        result = fetch_forcing_data_routed(
            geom, start_date, end_date, variables=variables, product=product
        )
        return result.to_dict()  # {"data": {...}, "meta": {...}}

    return _forcing_kernel


# Build the kernel at module import time (None if aihydro-core not available)
forcing_kernel = _make_forcing_kernel()
