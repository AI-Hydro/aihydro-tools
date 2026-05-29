"""
Curve Number (CN) Grid Generation — Analysis Layer

Creates NRCS Curve Number grids for watersheds by combining NLCD land cover
data with soil properties to characterize runoff potential.

Data retrieval functions (fetch_lulc_data, fetch_soil_data_polaris) are in
ai_hydro.data.landcover and ai_hydro.data.soil respectively.

Large-basin performance
~~~~~~~~~~~~~~~~~~~~~~~
For watersheds where the LULC raster exceeds ``_CN_CHUNK_TRIGGER`` cells
(default 10 M), the LULC × soil overlay is computed via
``aihydro_data.sampling.chunked_raster_apply`` instead of operating on the
full array at once.  The "joint" encoding packs both LULC class and soil
hydrologic group into a single float32 channel so that the single-raster
applier can be reused without API changes::

    joint_value = float(lulc_class * 10 + soil_group)   # e.g. 41→Group B: 412.0

A flat lookup array (size 1000) converts joint values back to CN values in
O(n) time with one vectorised numpy index.
"""

from __future__ import annotations

import logging
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
import warnings

log = logging.getLogger(__name__)

# Auto-trigger chunked CN overlay when the LULC raster exceeds this size.
_CN_CHUNK_TRIGGER = 10_000_000

try:
    import xarray as xr
    import geopandas as gpd
    from ai_hydro.data.landcover import fetch_lulc_data
    from ai_hydro.data.soil import fetch_soil_data_polaris
    _DEPS_AVAILABLE = True
except ImportError:
    _DEPS_AVAILABLE = False

# Conditional imports for visualization
try:
    import matplotlib
    matplotlib.use("Agg")  # headless — MCP server has no display
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    warnings.warn("matplotlib not available - visualizations will be disabled")

try:
    import folium
    from folium import raster_layers
    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False
    warnings.warn("folium not available - interactive maps will be disabled")


def create_curve_number_grid(
    gauge_id: str,
    year: int = 2019,
    resolution: int = 30,
    save_outputs: bool = True,
    output_dir: Optional[str] = None,
    output_formats: List[str] = None,
    create_visualizations: bool = True,
    output_prefix: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create comprehensive Curve Number grid for a USGS gauge watershed.
    
    This function performs complete CN grid generation including watershed delineation,
    LULC data retrieval, soil properties extraction, hydrologic group classification,
    CN grid creation, and optional visualizations.
    
    Parameters
    ----------
    gauge_id : str
        USGS gauge identifier (8-digit code, e.g., '03245500')
    year : int, optional
        Year of NLCD data to use (default: 2019)
    resolution : int, optional
        Spatial resolution in meters (default: 30m NLCD native)
    save_outputs : bool, optional
        Whether to save output files (default: True)
    output_dir : str, optional
        Output directory path. If None, uses './output/cn_grid_{gauge_id}'
    output_formats : list of str, optional
        Output formats to save: 'geotiff', 'netcdf', or both (default: ['geotiff', 'netcdf'])
    create_visualizations : bool, optional
        Whether to create PNG and HTML visualizations (default: True)
    output_prefix : str, optional
        Filename prefix for outputs (default: 'cn_grid')
    
    Returns
    -------
    dict
        Dictionary with the following structure:
        
        {
            'cn_grid': xarray.DataArray,
            
            'statistics': {
                'cn_mean': float,
                'cn_median': float,
                'cn_min': float,
                'cn_max': float,
                'cn_std': float,
                'cn_p10': float,
                'cn_p25': float,
                'cn_p75': float,
                'cn_p90': float
            },
            
            'cn_zones': {
                'percent_low_cn': float,              # CN < 70
                'percent_medium_cn': float,           # CN 70-85 (NOTE: 'medium' not 'moderate')
                'percent_high_cn': float,             # CN > 85
                'low_cn_interpretation': str,
                'medium_cn_interpretation': str,
                'high_cn_interpretation': str
            },
            
            'watershed_info': {
                'gauge_id': str,
                'gauge_name': str,
                'area_km2': float,
                'centroid_lat': float,
                'centroid_lon': float,
                'geometry': shapely.geometry.Polygon,
                'gdf': geopandas.GeoDataFrame
            },
            
            'lulc_stats': {
                'classes': list,       # NLCD class codes
                'counts': list,        # Pixel counts
                'percentages': list    # Percentage for each class
            },
            
            'soil_stats': {
                'soil_group_distribution': {
                    'A': int, 'B': int, 'C': int, 'D': int
                },
                'soil_group_percentages': {
                    'A': float, 'B': float, 'C': float, 'D': float
                }
            },
            
            'file_paths': {         # Only if save_outputs=True
                'watershed': str,
                'geotiff': str,
                'netcdf': str,
                'statistics': str,
                'png_map': str,
                'html_map': str
            },
            
            'visualizations': {     # Only if create_visualizations=True
                'figure': matplotlib.figure.Figure
            }
        }
        
        Access examples:
        - CN mean: result['statistics']['cn_mean']
        - Medium CN zone: result['cn_zones']['percent_medium_cn']
        - Land cover classes: result['lulc_stats']['classes']
        - Gauge name: result['watershed_info']['gauge_name']
    
    Examples
    --------
    >>> # Simple usage - automatic everything
    >>> result = create_curve_number_grid('03245500')
    >>> print(f"Mean CN: {result['statistics']['cn_mean']:.1f}")
    >>> print(f"High CN areas: {result['cn_zones']['percent_high_cn']:.1f}%")
    
    >>> # Custom parameters
    >>> result = create_curve_number_grid(
    ...     gauge_id='03245500',
    ...     year=2016,
    ...     resolution=10,
    ...     output_formats=['netcdf'],
    ...     create_visualizations=True
    ... )
    
    Notes
    -----
    - CN values are for Antecedent Moisture Condition II (AMC-II)
    - Soil groups: A (high infiltration) to D (very slow infiltration)
    - Uses NRCS CN lookup tables for NLCD classes
    - Outputs in WGS84 (EPSG:4326) projection
    """
    if not _DEPS_AVAILABLE:
        raise ImportError("curve number analysis requires: pip install aihydro-tools[analysis]")
    from ai_hydro.analysis.watershed import delineate_watershed

    # Set defaults
    if output_formats is None:
        output_formats = ['geotiff', 'netcdf']
    if output_prefix is None:
        output_prefix = 'cn_grid'
    
    # Setup output directory
    if output_dir is None:
        output_dir = f'./output/cn_grid_{gauge_id}'
    output_path = Path(output_dir)
    if save_outputs:
        output_path.mkdir(parents=True, exist_ok=True)
    
    print("="*70)
    print("CURVE NUMBER GRID GENERATION")
    print("="*70)
    print(f"Gauge ID: {gauge_id}")
    print(f"NLCD Year: {year}")
    print(f"Resolution: {resolution}m")
    print()
    
    # Step 1: Delineate watershed
    print("Step 1: Delineate watershed boundary")
    print("-" * 50)
    watershed_data = delineate_watershed(gauge_id=gauge_id)
    watershed_geom = watershed_data['geometry']
    watershed_gdf = watershed_data['gdf']
    
    print(f"  Watershed: {watershed_data['gauge_name']}")
    print(f"  Area: {watershed_data['area_km2']:.2f} km²")
    print(f"  Centroid: ({watershed_data['gauge_lon']:.4f}, {watershed_data['gauge_lat']:.4f})")
    print()
    
    # Step 2: Fetch LULC data
    print("Step 2: Fetch NLCD land cover data")
    print("-" * 50)
    lulc_data = fetch_lulc_data(watershed_gdf, resolution=resolution, year=year)
    cover_var = f'cover_{year}'
    print(f"  Retrieved {cover_var} with shape: {lulc_data[cover_var].shape}")
    
    # Get LULC statistics
    lulc_values = lulc_data[cover_var].values
    unique_classes, class_counts = np.unique(lulc_values[~np.isnan(lulc_values)], return_counts=True)
    lulc_stats = {
        'classes': unique_classes.tolist(),
        'counts': class_counts.tolist(),
        'percentages': (100 * class_counts / class_counts.sum()).tolist()
    }
    print(f"  Unique NLCD classes: {len(unique_classes)}")
    print()
    
    # Step 3: Fetch soil data
    print("Step 3: Fetch soil properties from Polaris")
    print("-" * 50)
    soil_data = fetch_soil_data_polaris(watershed_gdf)
    print(f"  Retrieved soil data variables: {list(soil_data.data_vars)}")
    print()
    
    # Step 4: Classify soil groups and create CN grid
    print("Step 4: Create Curve Number grid")
    print("-" * 50)
    cn_grid, soil_group_grid, soil_stats = _create_cn_grid_from_data(
        lulc_data, soil_data, year, resolution,
        watershed_geom=watershed_geom,
    )
    print()

    # Step 5: Calculate statistics
    print("Step 5: Calculate statistics and zones")
    print("-" * 50)
    valid_cn = cn_grid.values[~np.isnan(cn_grid.values)]
    statistics = {
        'cn_mean': float(valid_cn.mean()),
        'cn_median': float(np.median(valid_cn)),
        'cn_min': float(valid_cn.min()),
        'cn_max': float(valid_cn.max()),
        'cn_std': float(valid_cn.std()),
        'cn_p10': float(np.percentile(valid_cn, 10)),
        'cn_p25': float(np.percentile(valid_cn, 25)),
        'cn_p75': float(np.percentile(valid_cn, 75)),
        'cn_p90': float(np.percentile(valid_cn, 90))
    }

    # Classify CN zones
    cn_zones = _classify_cn_zones(cn_grid.values)

    print(f"  CN Statistics:")
    print(f"    Mean: {statistics['cn_mean']:.1f}")
    print(f"    Median: {statistics['cn_median']:.1f}")
    print(f"    Range: {statistics['cn_min']:.0f} - {statistics['cn_max']:.0f}")
    print(f"    Std: {statistics['cn_std']:.1f}")
    print(f"  CN Zones:")
    print(f"    Low CN (<70): {cn_zones['percent_low_cn']:.1f}%")
    print(f"    Medium CN (70-85): {cn_zones['percent_medium_cn']:.1f}%")
    print(f"    High CN (>85): {cn_zones['percent_high_cn']:.1f}%")
    print()

    # Step 6: Save outputs
    file_paths = {}
    if save_outputs:
        print("Step 6: Save outputs")
        print("-" * 50)

        # Save watershed boundary
        watershed_path = output_path / f'{output_prefix}_watershed_{gauge_id}.gpkg'
        watershed_gdf.to_file(watershed_path, driver='GPKG')
        file_paths['watershed'] = str(watershed_path)
        print(f"  Saved watershed: {watershed_path.name}")
        
        # Save CN grid in requested formats
        if 'geotiff' in output_formats:
            tif_path = output_path / f'{output_prefix}_{gauge_id}.tif'
            cn_grid.rio.to_raster(tif_path, driver='GTiff', compress='lzw')
            file_paths['geotiff'] = str(tif_path)
            print(f"  Saved GeoTIFF: {tif_path.name}")
        
        if 'netcdf' in output_formats:
            nc_path = output_path / f'{output_prefix}_{gauge_id}.nc'
            # Add metadata
            cn_grid.attrs['gauge_id'] = gauge_id
            cn_grid.attrs['gauge_name'] = watershed_data['gauge_name']
            cn_grid.attrs['creation_date'] = str(np.datetime64('today'))
            cn_grid.to_netcdf(nc_path)
            file_paths['netcdf'] = str(nc_path)
            print(f"  Saved NetCDF: {nc_path.name}")
        
        # Save statistics as JSON
        import json
        stats_path = output_path / f'{output_prefix}_statistics_{gauge_id}.json'
        stats_output = {
            'gauge_id': gauge_id,
            'gauge_name': watershed_data['gauge_name'],
            'area_km2': watershed_data['area_km2'],
            'cn_statistics': statistics,
            'cn_zones': cn_zones,
            'lulc_year': year,
            'resolution_m': resolution
        }
        with open(stats_path, 'w') as f:
            json.dump(stats_output, f, indent=2)
        file_paths['statistics'] = str(stats_path)
        print(f"  Saved statistics: {stats_path.name}")
        print()
    
    # Step 7: Create visualizations
    visualizations = {}
    if create_visualizations:
        print("Step 7: Create visualizations")
        print("-" * 50)
        
        # Static PNG map
        if HAS_MATPLOTLIB:
            fig = _create_static_visualization(cn_grid, watershed_gdf, gauge_id)
            visualizations['figure'] = fig
            
            if save_outputs:
                png_path = output_path / f'{output_prefix}_{gauge_id}.png'
                fig.savefig(png_path, dpi=300, bbox_inches='tight')
                file_paths['png_map'] = str(png_path)
                print(f"  Saved PNG map: {png_path.name}")
                plt.close(fig)
        
        # Interactive HTML map
        if HAS_FOLIUM and save_outputs:
            html_path = output_path / f'{output_prefix}_{gauge_id}.html'
            _create_interactive_map(cn_grid, watershed_gdf, html_path, gauge_id)
            file_paths['html_map'] = str(html_path)
            print(f"  Saved HTML map: {html_path.name}")
        
        print()
    
    # Summary
    print("="*70)
    print("COMPLETED SUCCESSFULLY!")
    print("="*70)
    if save_outputs:
        print(f"Output directory: {output_path.absolute()}")
        print(f"Files saved: {len(file_paths)}")
    print()
    
    # Return comprehensive results
    return {
        'cn_grid': cn_grid,
        'statistics': statistics,
        'cn_zones': cn_zones,
        'watershed_info': {
            'gauge_id': watershed_data['gauge_id'],
            'gauge_name': watershed_data['gauge_name'],
            'area_km2': watershed_data['area_km2'],
            'centroid_lat': watershed_data['gauge_lat'],
            'centroid_lon': watershed_data['gauge_lon'],
            'geometry': watershed_geom,
            'gdf': watershed_gdf
        },
        'lulc_stats': lulc_stats,
        'soil_stats': soil_stats,
        'file_paths': file_paths,
        'files_saved': list(file_paths.values()),
        'visualizations': visualizations
    }


def create_curve_number_grid_from_geometry(
    geometry,
    year: int = 2019,
    resolution: int = 30,
    save_outputs: bool = True,
    output_dir: Optional[str] = None,
    output_formats: List[str] = None,
    create_visualizations: bool = True,
    output_prefix: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create comprehensive Curve Number grid from custom watershed geometry.
    
    This function accepts various geometry formats (shapefile, GeoJSON, GeoDataFrame,
    or shapely geometry) and creates a CN grid following the same methodology as
    the gauge-based function. Ideal for custom watersheds, delineated boundaries,
    or non-USGS catchments.
    
    Parameters
    ----------
    geometry : str, Path, gpd.GeoDataFrame, or shapely.Polygon
        Watershed geometry in one of these formats:
        - str/Path: Path to shapefile (.shp) or GeoJSON (.geojson, .json)
        - gpd.GeoDataFrame: Watershed as GeoDataFrame
        - shapely.Polygon: Watershed boundary polygon
        Must be in WGS84 (EPSG:4326) or will be reprojected
    year : int, optional
        Year of NLCD data to use (default: 2019)
    resolution : int, optional
        Spatial resolution in meters (default: 30m)
    save_outputs : bool, optional
        Whether to save output files (default: True)
    output_dir : str, optional
        Output directory path. If None, uses './output/cn_grid_custom'
    output_formats : list of str, optional
        Output formats: 'geotiff', 'netcdf', or both (default: ['geotiff', 'netcdf'])
    create_visualizations : bool, optional
        Whether to create PNG and HTML visualizations (default: True)
    output_prefix : str, optional
        Filename prefix for outputs (default: 'cn_grid_custom')
    
    Returns
    -------
    dict
        Dictionary with the following structure (same as create_curve_number_grid):
        
        {
            'cn_grid': xarray.DataArray,
            
            'statistics': {
                'cn_mean': float,
                'cn_median': float,
                'cn_min': float,
                'cn_max': float,
                'cn_std': float,
                'cn_p10': float,
                'cn_p25': float,
                'cn_p75': float,
                'cn_p90': float
            },
            
            'cn_zones': {
                'percent_low_cn': float,              # CN < 70
                'percent_medium_cn': float,           # CN 70-85 (NOTE: 'medium' not 'moderate')
                'percent_high_cn': float,             # CN > 85
                'low_cn_interpretation': str,
                'medium_cn_interpretation': str,
                'high_cn_interpretation': str
            },
            
            'watershed_info': {
                'source': 'custom_geometry',
                'area_km2': float,
                'centroid_lat': float,
                'centroid_lon': float,
                'geometry': shapely.geometry.Polygon,
                'gdf': geopandas.GeoDataFrame
            },
            
            'lulc_stats': {
                'classes': list,       # NLCD class codes
                'counts': list,        # Pixel counts
                'percentages': list    # Percentage for each class
            },
            
            'soil_stats': {
                'soil_group_distribution': {
                    'A': int, 'B': int, 'C': int, 'D': int
                },
                'soil_group_percentages': {
                    'A': float, 'B': float, 'C': float, 'D': float
                }
            },
            
            'file_paths': {         # Only if save_outputs=True
                'watershed': str,
                'geotiff': str,
                'netcdf': str,
                'statistics': str,
                'png_map': str,
                'html_map': str
            },
            
            'visualizations': {     # Only if create_visualizations=True
                'figure': matplotlib.figure.Figure
            }
        }
        
        Access examples:
        - CN mean: result['statistics']['cn_mean']
        - Medium CN zone: result['cn_zones']['percent_medium_cn']
        - Land cover classes: result['lulc_stats']['classes']
        - Watershed area: result['watershed_info']['area_km2']
    
    Examples
    --------
    >>> # From shapefile
    >>> result = create_curve_number_grid_from_geometry('watershed.shp')
    
    >>> # From GeoJSON
    >>> result = create_curve_number_grid_from_geometry('watershed.geojson')
    
    >>> # From delineated watershed
    >>> from ai_hydro.analysis.watershed import delineate_watershed
    >>> ws = delineate_watershed('01031500')
    >>> result = create_curve_number_grid_from_geometry(ws['geometry'])
    
    >>> # From GeoDataFrame with custom settings
    >>> result = create_curve_number_grid_from_geometry(
    ...     watershed_gdf,
    ...     year=2016,
    ...     resolution=10,
    ...     output_dir='./my_cn_analysis',
    ...     output_prefix='custom_watershed'
    ... )
    
    Notes
    -----
    - CN values are for Antecedent Moisture Condition II (AMC-II)
    - Uses same methodology as gauge-based function
    - Accepts any geometry format - automatically handles conversion
    - Outputs in WGS84 (EPSG:4326) projection
    """
    if not _DEPS_AVAILABLE:
        raise ImportError("curve number analysis requires: pip install aihydro-tools[analysis]")
    from pathlib import Path

    # Set defaults
    if output_formats is None:
        output_formats = ['geotiff', 'netcdf']
    if output_prefix is None:
        output_prefix = 'cn_grid_custom'
    
    # Setup output directory
    if output_dir is None:
        output_dir = './output/cn_grid_custom'
    output_path = Path(output_dir)
    if save_outputs:
        output_path.mkdir(parents=True, exist_ok=True)
    
    print("="*70)
    print("CURVE NUMBER GRID GENERATION (CUSTOM GEOMETRY)")
    print("="*70)
    print(f"Geometry Type: {type(geometry).__name__}")
    print(f"NLCD Year: {year}")
    print(f"Resolution: {resolution}m")
    print()
    
    # Convert geometry to GeoDataFrame
    print("Step 1: Process input geometry")
    print("-" * 50)
    watershed_gdf = _convert_geometry_to_geodataframe(geometry)
    watershed_geom = watershed_gdf.geometry.iloc[0]
    
    # Calculate area
    area_km2 = watershed_gdf.to_crs(epsg=5070).area.iloc[0] / 1e6  # Convert m² to km²
    centroid = watershed_geom.centroid
    
    print(f"  Geometry processed successfully")
    print(f"  Area: {area_km2:.2f} km²")
    print(f"  Centroid: ({centroid.x:.4f}, {centroid.y:.4f})")
    print()
    
    # Step 2: Fetch LULC data
    print("Step 2: Fetch NLCD land cover data")
    print("-" * 50)
    lulc_data = fetch_lulc_data(watershed_gdf, resolution=resolution, year=year)
    cover_var = f'cover_{year}'
    print(f"  Retrieved {cover_var} with shape: {lulc_data[cover_var].shape}")
    
    # Get LULC statistics
    lulc_values = lulc_data[cover_var].values
    unique_classes, class_counts = np.unique(lulc_values[~np.isnan(lulc_values)], return_counts=True)
    lulc_stats = {
        'classes': unique_classes.tolist(),
        'counts': class_counts.tolist(),
        'percentages': (100 * class_counts / class_counts.sum()).tolist()
    }
    print(f"  Unique NLCD classes: {len(unique_classes)}")
    print()
    
    # Step 3: Fetch soil data
    print("Step 3: Fetch soil properties from Polaris")
    print("-" * 50)
    soil_data = fetch_soil_data_polaris(watershed_gdf)
    print(f"  Retrieved soil data variables: {list(soil_data.data_vars)}")
    print()
    
    # Step 4: Classify soil groups and create CN grid
    print("Step 4: Create Curve Number grid")
    print("-" * 50)
    cn_grid, soil_group_grid, soil_stats = _create_cn_grid_from_data(
        lulc_data, soil_data, year, resolution,
        watershed_geom=watershed_geom,
    )
    print()

    # Step 5: Calculate statistics
    print("Step 5: Calculate statistics and zones")
    print("-" * 50)
    valid_cn = cn_grid.values[~np.isnan(cn_grid.values)]
    statistics = {
        'cn_mean': float(valid_cn.mean()),
        'cn_median': float(np.median(valid_cn)),
        'cn_min': float(valid_cn.min()),
        'cn_max': float(valid_cn.max()),
        'cn_std': float(valid_cn.std()),
        'cn_p10': float(np.percentile(valid_cn, 10)),
        'cn_p25': float(np.percentile(valid_cn, 25)),
        'cn_p75': float(np.percentile(valid_cn, 75)),
        'cn_p90': float(np.percentile(valid_cn, 90))
    }

    # Classify CN zones
    cn_zones = _classify_cn_zones(cn_grid.values)

    print(f"  CN Statistics:")
    print(f"    Mean: {statistics['cn_mean']:.1f}")
    print(f"    Median: {statistics['cn_median']:.1f}")
    print(f"    Range: {statistics['cn_min']:.0f} - {statistics['cn_max']:.0f}")
    print(f"    Std: {statistics['cn_std']:.1f}")
    print(f"  CN Zones:")
    print(f"    Low CN (<70): {cn_zones['percent_low_cn']:.1f}%")
    print(f"    Medium CN (70-85): {cn_zones['percent_medium_cn']:.1f}%")
    print(f"    High CN (>85): {cn_zones['percent_high_cn']:.1f}%")
    print()

    # Step 6: Save outputs
    file_paths = {}
    if save_outputs:
        print("Step 6: Save outputs")
        print("-" * 50)

        # Save watershed boundary
        watershed_path = output_path / f'{output_prefix}_watershed.gpkg'
        watershed_gdf.to_file(watershed_path, driver='GPKG')
        file_paths['watershed'] = str(watershed_path)
        print(f"  Saved watershed: {watershed_path.name}")
        
        # Save CN grid in requested formats
        if 'geotiff' in output_formats:
            tif_path = output_path / f'{output_prefix}.tif'
            cn_grid.rio.to_raster(tif_path, driver='GTiff', compress='lzw')
            file_paths['geotiff'] = str(tif_path)
            print(f"  Saved GeoTIFF: {tif_path.name}")
        
        if 'netcdf' in output_formats:
            nc_path = output_path / f'{output_prefix}.nc'
            # Add metadata
            cn_grid.attrs['source'] = 'custom_geometry'
            cn_grid.attrs['creation_date'] = str(np.datetime64('today'))
            cn_grid.to_netcdf(nc_path)
            file_paths['netcdf'] = str(nc_path)
            print(f"  Saved NetCDF: {nc_path.name}")
        
        # Save statistics as JSON
        import json
        stats_path = output_path / f'{output_prefix}_statistics.json'
        stats_output = {
            'source': 'custom_geometry',
            'area_km2': area_km2,
            'cn_statistics': statistics,
            'cn_zones': cn_zones,
            'lulc_year': year,
            'resolution_m': resolution
        }
        with open(stats_path, 'w') as f:
            json.dump(stats_output, f, indent=2)
        file_paths['statistics'] = str(stats_path)
        print(f"  Saved statistics: {stats_path.name}")
        print()
    
    # Step 7: Create visualizations
    visualizations = {}
    if create_visualizations:
        print("Step 7: Create visualizations")
        print("-" * 50)
        
        # Static PNG map
        if HAS_MATPLOTLIB:
            fig = _create_static_visualization(cn_grid, watershed_gdf, 'Custom Geometry')
            visualizations['figure'] = fig
            
            if save_outputs:
                png_path = output_path / f'{output_prefix}.png'
                fig.savefig(png_path, dpi=300, bbox_inches='tight')
                file_paths['png_map'] = str(png_path)
                print(f"  Saved PNG map: {png_path.name}")
                plt.close(fig)
        
        # Interactive HTML map
        if HAS_FOLIUM and save_outputs:
            html_path = output_path / f'{output_prefix}.html'
            _create_interactive_map(cn_grid, watershed_gdf, html_path, 'Custom Geometry')
            file_paths['html_map'] = str(html_path)
            print(f"  Saved HTML map: {html_path.name}")
        
        print()
    
    # Summary
    print("="*70)
    print("COMPLETED SUCCESSFULLY!")
    print("="*70)
    if save_outputs:
        print(f"Output directory: {output_path.absolute()}")
        print(f"Files saved: {len(file_paths)}")
    print()
    
    # Return comprehensive results
    return {
        'cn_grid': cn_grid,
        'statistics': statistics,
        'cn_zones': cn_zones,
        'watershed_info': {
            'source': 'custom_geometry',
            'area_km2': area_km2,
            'centroid_lat': centroid.y,
            'centroid_lon': centroid.x,
            'geometry': watershed_geom,
            'gdf': watershed_gdf
        },
        'lulc_stats': lulc_stats,
        'soil_stats': soil_stats,
        'file_paths': file_paths,
        'files_saved': list(file_paths.values()),
        'visualizations': visualizations
    }


# ============================================================================
# Internal Helper Functions (not exported to RAG)
# ============================================================================

def _convert_geometry_to_geodataframe(geometry) -> gpd.GeoDataFrame:
    """
    Convert various geometry formats to GeoDataFrame.
    
    Accepts:
    - File paths (str/Path): .shp, .geojson, .json
    - GeoDataFrame
    - Shapely Polygon/MultiPolygon
    
    Returns GeoDataFrame in WGS84 (EPSG:4326).
    """
    from pathlib import Path
    from shapely.geometry import Polygon, MultiPolygon
    
    # If already GeoDataFrame
    if isinstance(geometry, gpd.GeoDataFrame):
        gdf = geometry.copy()
        if gdf.crs is None:
            gdf.set_crs(epsg=4326, inplace=True)
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        return gdf
    
    # If file path (string or Path)
    if isinstance(geometry, (str, Path)):
        path = Path(geometry)
        
        if not path.exists():
            raise FileNotFoundError(f"Geometry file not found: {path}")
        
        # Read file based on extension
        if path.suffix.lower() in ['.shp', '.geojson', '.json', '.gpkg']:
            gdf = gpd.read_file(path)
            if gdf.crs is None:
                gdf.set_crs(epsg=4326, inplace=True)
            elif gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(epsg=4326)
            return gdf
        else:
            raise ValueError(f"Unsupported file format: {path.suffix}. "
                           "Supported formats: .shp, .geojson, .json, .gpkg")
    
    # If shapely Polygon or MultiPolygon
    if isinstance(geometry, (Polygon, MultiPolygon)):
        gdf = gpd.GeoDataFrame({'geometry': [geometry]}, crs='EPSG:4326')
        return gdf
    
    # If GeoSeries
    if isinstance(geometry, gpd.GeoSeries):
        gdf = gpd.GeoDataFrame({'geometry': geometry}, crs=geometry.crs)
        if gdf.crs is None:
            gdf.set_crs(epsg=4326, inplace=True)
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        return gdf
    
    raise TypeError(f"Unsupported geometry type: {type(geometry)}. "
                   "Expected: str/Path (file), GeoDataFrame, GeoSeries, or shapely Polygon/MultiPolygon")


def _build_joint_cn_lookup(cn_table: Dict) -> np.ndarray:
    """Build a flat lookup array indexed by joint key = NLCD * 10 + soil_group.

    NLCD classes are 11–95, soil groups 1–4, so joint values are 111–954.
    We size the array at 1000 to have comfortable headroom.

    Parameters
    ----------
    cn_table : dict
        Mapping ``(nlcd_class, soil_group) -> cn_value``.

    Returns
    -------
    np.ndarray  shape (1000,), dtype float32
        ``lookup[nlcd * 10 + soil_group]`` = CN value (NaN if not in table).
    """
    lookup = np.full(1000, np.nan, dtype=np.float32)
    for (nlcd, sg), cn_val in cn_table.items():
        key = int(nlcd) * 10 + int(sg)
        if 0 <= key < 1000:
            lookup[key] = float(cn_val)
    return lookup


def _create_cn_grid_from_data(
    lulc_data: xr.Dataset,
    soil_data: xr.Dataset,
    year: int,
    resolution: int,
    watershed_geom=None,
) -> Tuple[xr.DataArray, np.ndarray, Dict]:
    """Create CN grid from LULC and soil data.

    Internal function that combines land cover and soil properties to generate
    the Curve Number grid.

    The LULC × soil overlay is vectorised via a flat lookup array (O(n),
    single pass) rather than the former nested-loop approach (O(n_classes ×
    n_soil_groups × n_pixels) with repeated boolean mask scans).  For rasters
    larger than ``_CN_CHUNK_TRIGGER`` cells, the overlay is additionally
    streamed through ``chunked_raster_apply`` to cap peak memory usage.

    Parameters
    ----------
    watershed_geom : shapely.geometry.BaseGeometry or None
        Watershed polygon in the same CRS as the LULC raster.  Used only by
        the chunked path for chip pruning; ignored when the raster is small
        enough for single-pass execution.
    """
    # Extract land cover
    cover_var = f'cover_{year}'
    lulc = lulc_data[cover_var]

    # Extract soil properties
    soil_vars = list(soil_data.data_vars)

    # Map to correct variable names (Polaris naming can vary)
    if 'sand_0_5cm_mean' in soil_vars:
        sand_da = soil_data['sand_0_5cm_mean']
        silt_da = soil_data['silt_0_5cm_mean']
        clay_da = soil_data['clay_0_5cm_mean']
        ksat_da = soil_data.get('ksat_0_5cm_mean', None)
    elif 'sand_5' in soil_vars:
        sand_da = soil_data['sand_5']
        silt_da = soil_data['silt_5']
        clay_da = soil_data['clay_5']
        ksat_da = soil_data.get('ksat_5', None)
    else:
        raise ValueError(f"Unexpected soil variable names: {soil_vars}")

    # Resample soil data to match LULC grid if needed
    if sand_da.shape != lulc.shape:
        print(f"  Resampling soil data from {sand_da.shape} to match LULC grid {lulc.shape}...")
        sand_da = sand_da.rio.reproject_match(lulc)
        silt_da = silt_da.rio.reproject_match(lulc)
        clay_da = clay_da.rio.reproject_match(lulc)
        if ksat_da is not None:
            ksat_da = ksat_da.rio.reproject_match(lulc)

    # Extract values
    sand = sand_da.values
    silt = silt_da.values
    clay = clay_da.values
    ksat = ksat_da.values if ksat_da is not None else None
    lulc_values = lulc.values

    # Classify soil hydrologic groups
    print("  Classifying soil hydrologic groups...")
    soil_groups, soil_stats = _classify_soil_hydrologic_group(sand, silt, clay, ksat)

    # Create CN lookup table + flat lookup array
    cn_table = _create_cn_lookup_table()
    lookup = _build_joint_cn_lookup(cn_table)

    print("  Applying CN lookup table...")

    n_pixels = lulc.size
    if n_pixels > _CN_CHUNK_TRIGGER and watershed_geom is not None:
        # ------------------------------------------------------------------
        # Chunked path — encode LULC + soil_group into a single float32
        # channel so that chunked_raster_apply (single-raster API) can be
        # reused without modification.
        #
        # Joint encoding: joint = lulc_class * 10 + soil_group
        # NLCD classes are 11–95, soil groups 1–4 → keys 111–954, well
        # inside the lookup array bounds.
        # ------------------------------------------------------------------
        log.info(
            "curve_number._create_cn_grid_from_data: %d cells > threshold %d "
            "— using chunked CN overlay",
            n_pixels, _CN_CHUNK_TRIGGER,
        )
        try:
            from aihydro_data.sampling import chunked_raster_apply

            lulc_safe = np.where(np.isnan(lulc_values), 0, lulc_values).astype(np.float32)
            sg_safe = soil_groups.astype(np.float32)
            joint_arr = lulc_safe * 10.0 + sg_safe
            joint_da = xr.DataArray(
                joint_arr, dims=lulc.dims, coords=lulc.coords,
                attrs=lulc.attrs,
            )

            # Capture lookup in closure (read-only; safe for multi-threaded use
            # since numpy fancy indexing is thread-safe on CPython).
            _lookup = lookup

            def _cn_fn(arr: np.ndarray, mask: np.ndarray) -> np.ndarray:
                keys = np.where(
                    (arr > 0) & np.isfinite(arr),
                    arr.astype(np.int32),
                    0,
                )
                result = _lookup[np.clip(keys, 0, len(_lookup) - 1)]
                # Pixels with key == 0 (NoData / unclassified) stay NaN.
                result = np.where(keys > 0, result, np.nan)
                return result.astype(np.float32)

            cn_da = chunked_raster_apply(
                joint_da, watershed_geom, _cn_fn,
                kernel_pad=0,
                auto_trigger_size=0,   # always chunked
                fill_value=np.nan,
            )
            cn_grid_values = cn_da.values

        except Exception as _ce:
            log.warning(
                "Chunked CN overlay failed (%s); falling back to vectorised single-pass",
                _ce,
            )
            cn_grid_values = _vectorised_cn_lookup(lulc_values, soil_groups, lookup)

    else:
        # ------------------------------------------------------------------
        # Single-pass vectorised path (small raster or no geometry for
        # chunked pruning).
        # ------------------------------------------------------------------
        cn_grid_values = _vectorised_cn_lookup(lulc_values, soil_groups, lookup)

    # Create xarray DataArray
    cn_grid = xr.DataArray(
        cn_grid_values,
        coords=lulc.coords,
        dims=lulc.dims,
        attrs={
            'long_name': 'NRCS Curve Number',
            'units': 'dimensionless',
            'description': 'SCS Curve Number for AMC-II conditions',
            'source': 'NLCD land cover + Polaris soil data',
            'year': year,
            'resolution_m': resolution,
        },
    )

    # Add CRS
    try:
        cn_grid.rio.write_crs(lulc.rio.crs, inplace=True)
    except Exception:
        pass

    # Print statistics
    valid_cn = cn_grid_values[~np.isnan(cn_grid_values)]
    if valid_cn.size > 0:
        print(f"  CN Grid created:")
        print(f"    Mean: {valid_cn.mean():.1f}")
        print(f"    Range: {valid_cn.min():.0f} - {valid_cn.max():.0f}")

    return cn_grid, soil_groups, soil_stats


def _vectorised_cn_lookup(
    lulc_values: np.ndarray,
    soil_groups: np.ndarray,
    lookup: np.ndarray,
) -> np.ndarray:
    """Apply the CN lookup table via vectorised numpy indexing.

    Parameters
    ----------
    lulc_values, soil_groups : np.ndarray
        Co-registered arrays of NLCD class codes and soil hydrologic groups
        (1–4), both shape (H, W).
    lookup : np.ndarray
        Flat lookup array built by :func:`_build_joint_cn_lookup`.

    Returns
    -------
    np.ndarray  shape (H, W), dtype float32
    """
    lulc_safe = np.where(np.isnan(lulc_values), 0, lulc_values).astype(np.int32)
    sg_safe = soil_groups.astype(np.int32)
    keys = lulc_safe * 10 + sg_safe
    cn_values = lookup[np.clip(keys, 0, len(lookup) - 1)]
    # Pixels where lulc was NaN or key == 0 stay NaN.
    cn_values = np.where(keys > 0, cn_values, np.nan)
    return cn_values.astype(np.float32)


def _classify_soil_hydrologic_group(
    sand: np.ndarray,
    silt: np.ndarray,
    clay: np.ndarray,
    ksat: Optional[np.ndarray] = None
) -> Tuple[np.ndarray, Dict]:
    """
    Classify soils into NRCS hydrologic groups (A, B, C, D) based on texture.
    
    Internal function for soil classification.
    
    Returns
    -------
    soil_group : np.ndarray
        Soil hydrologic group as integers (1=A, 2=B, 3=C, 4=D)
    stats : dict
        Distribution statistics of soil groups
    """
    # Initialize with group D (most restrictive)
    soil_group = np.full_like(sand, 4, dtype=np.int32)
    
    # Group A: High sand content (>70%), low clay (<10%)
    mask_A = (sand > 70) & (clay < 10)
    soil_group[mask_A] = 1
    
    # Group B: Moderate sand (50-70%) or silt dominant with low clay
    mask_B = ((sand >= 50) & (sand <= 70) & (clay < 20)) | \
             ((silt > 50) & (clay < 27))
    soil_group[mask_B] = 2
    
    # Group C: Higher clay content (20-40%) or sandy clay loam
    mask_C = ((clay >= 20) & (clay < 40)) & (~mask_A) & (~mask_B)
    soil_group[mask_C] = 3
    
    # Group D: High clay content (>40%) - already initialized as 4
    
    # If ksat is available, refine classification
    if ksat is not None:
        # Very high infiltration -> Group A
        soil_group[ksat > 15] = 1
        # High infiltration -> Group B
        soil_group[(ksat > 5) & (ksat <= 15) & (soil_group > 2)] = 2
        # Moderate infiltration -> Group C
        soil_group[(ksat > 1.5) & (ksat <= 5) & (soil_group > 3)] = 3
        # Low infiltration (<1.5 cm/hr) -> Group D (already set)
    
    # Calculate statistics
    unique_groups, counts = np.unique(soil_group[~np.isnan(sand)], return_counts=True)
    group_names = {1: 'A', 2: 'B', 3: 'C', 4: 'D'}
    
    stats = {
        'soil_group_distribution': {},
        'soil_group_percentages': {}
    }
    
    print(f"    Soil group distribution:")
    for group, count in zip(unique_groups, counts):
        pct = 100 * count / counts.sum()
        group_name = group_names[int(group)]
        stats['soil_group_distribution'][group_name] = int(count)
        stats['soil_group_percentages'][group_name] = float(pct)
        print(f"      Group {group_name}: {pct:.1f}%")
    
    return soil_group, stats


def _create_cn_lookup_table() -> Dict[Tuple[int, int], int]:
    """
    Create NRCS Curve Number lookup table for NLCD classes and soil groups.
    
    Internal function - CN lookup table.
    
    Returns
    -------
    dict
        Dictionary mapping (NLCD_class, soil_group) -> CN value
    """
    cn_table = {}
    
    # NLCD classes and corresponding CN values for soil groups A, B, C, D
    nlcd_cn_values = {
        # Water (0% impervious assumed for wetlands)
        11: [100, 100, 100, 100],  # Open Water
        12: [100, 100, 100, 100],  # Perennial Ice/Snow
        
        # Developed (urban) areas
        21: [49, 69, 79, 84],  # Developed, Open Space (<20% impervious)
        22: [61, 75, 83, 87],  # Developed, Low Intensity (20-49% impervious)
        23: [72, 82, 88, 91],  # Developed, Medium Intensity (50-79% impervious)
        24: [89, 92, 94, 95],  # Developed, High Intensity (80-100% impervious)
        
        # Barren
        31: [77, 86, 91, 94],  # Barren Land (Rock/Sand/Clay)
        
        # Forest
        41: [36, 60, 73, 79],  # Deciduous Forest
        42: [36, 60, 73, 79],  # Evergreen Forest
        43: [36, 60, 73, 79],  # Mixed Forest
        
        # Shrubland/Grassland
        52: [35, 56, 70, 77],  # Shrub/Scrub
        71: [49, 69, 79, 84],  # Grassland/Herbaceous
        
        # Agriculture
        81: [67, 78, 85, 89],  # Pasture/Hay
        82: [67, 78, 85, 89],  # Cultivated Crops
        
        # Wetlands
        90: [80, 87, 93, 95],  # Woody Wetlands
        95: [80, 87, 93, 95],  # Emergent Herbaceous Wetlands
    }
    
    # Build lookup table for all combinations
    for nlcd_class, cn_values in nlcd_cn_values.items():
        for soil_group in range(1, 5):  # Groups A, B, C, D (1-4)
            cn_table[(nlcd_class, soil_group)] = cn_values[soil_group - 1]
    
    return cn_table


def _classify_cn_zones(cn_array: np.ndarray) -> Dict[str, float]:
    """
    Classify CN grid into runoff potential zones.
    
    Internal function for zone classification.
    """
    valid_cn = cn_array[~np.isnan(cn_array)]
    total_pixels = len(valid_cn)
    
    low_cn = np.sum(valid_cn < 70)
    medium_cn = np.sum((valid_cn >= 70) & (valid_cn <= 85))
    high_cn = np.sum(valid_cn > 85)
    
    return {
        'percent_low_cn': 100 * low_cn / total_pixels,
        'percent_medium_cn': 100 * medium_cn / total_pixels,
        'percent_high_cn': 100 * high_cn / total_pixels,
        'low_cn_interpretation': 'Low runoff potential - good infiltration',
        'medium_cn_interpretation': 'Moderate runoff potential',
        'high_cn_interpretation': 'High runoff potential - poor infiltration'
    }


def _create_static_visualization(
    cn_grid: xr.DataArray,
    watershed_gdf: gpd.GeoDataFrame,
    gauge_id: str
) -> plt.Figure:
    """
    Create static PNG visualization of CN grid.
    
    Internal function for visualization.
    """
    fig, ax = plt.subplots(figsize=(12, 10))
    
    # Plot CN grid
    im = cn_grid.plot(
        ax=ax,
        cmap='RdYlGn_r',  # Red (high CN) to Green (low CN)
        vmin=30,
        vmax=100,
        cbar_kwargs={'label': 'Curve Number', 'shrink': 0.8}
    )
    
    # Overlay watershed boundary
    watershed_gdf.boundary.plot(ax=ax, color='black', linewidth=2, label='Watershed')
    
    ax.set_title(f'Curve Number Grid - Gauge {gauge_id}', 
                 fontsize=14, fontweight='bold')
    ax.set_xlabel('Longitude')
    ax.set_ylabel('Latitude')
    ax.legend(loc='upper right')
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    
    return fig


def _create_interactive_map(
    cn_grid: xr.DataArray,
    watershed_gdf: gpd.GeoDataFrame,
    output_path: Path,
    gauge_id: str
) -> None:
    """
    Interactive HTML map of the CN grid via Folium.

    Implementation notes / bug-fix history:

    1. CRS handling. The cn_grid comes out of NLCD-derived computation in
       EPSG:5070 (CONUS Albers), with x/y in metres. The previous version
       treated cn_grid.x/.y as lat/lon, which put bounds millions of
       degrees off the equator and folium silently drew nothing. We now
       reproject bounds through rasterio.warp.transform_bounds and force
       the watershed GeoDataFrame into WGS84 before serialising.

    2. Actual overlay. The previous version computed `bounds` but never
       added an ImageOverlay layer — so the map had only the watershed
       outline and a popup, no raster. We now (a) colormap the array to
       an RGBA PNG, (b) downsample to ≤2000 px on the longest side, and
       (c) embed the PNG as a base64 data URI inside a raw Leaflet
       imageOverlay script block.  Using a data URI (rather than a
       relative file path) is necessary because the VS Code HTML preview
       loads HTML via `srcdoc`, which gives the iframe no base URL —
       relative paths simply 404.
    """
    import json
    import numpy as np
    import matplotlib.pyplot as plt
    from matplotlib import colors as mpl_colors

    # ── Watershed boundary in WGS84 for folium ─────────────────────────
    if watershed_gdf.crs is None:
        watershed_gdf = watershed_gdf.set_crs(epsg=4326)
    if watershed_gdf.crs.to_epsg() != 4326:
        watershed_gdf = watershed_gdf.to_crs(epsg=4326)

    # Map center from the watershed (use a representative point for
    # multipolygons since .centroid can fall outside the geometry)
    rep = watershed_gdf.geometry.representative_point().iloc[0]
    center_lat, center_lon = rep.y, rep.x

    m = folium.Map(location=[center_lat, center_lon], zoom_start=9,
                   tiles='OpenStreetMap')
    folium.TileLayer('cartodbpositron', name='Light').add_to(m)
    folium.TileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        attr='Esri', name='Satellite', overlay=False, control=True,
    ).add_to(m)

    # Watershed boundary
    folium.GeoJson(
        watershed_gdf.to_json(),
        name='Watershed Boundary',
        style_function=lambda _x: {
            'fillColor': 'transparent', 'color': 'black',
            'weight': 2.5, 'opacity': 0.9, 'fillOpacity': 0,
        },
    ).add_to(m)

    # ── Reproject bounds to WGS84 ──────────────────────────────────────
    # cn_grid carries its own CRS via rioxarray (`rio.crs`); fall back to
    # the .crs attr if set, then to EPSG:5070 which is the NLCD default.
    try:
        src_crs = cn_grid.rio.crs
    except Exception:
        src_crs = None
    if src_crs is None:
        src_crs = cn_grid.attrs.get('crs') or 'EPSG:5070'

    # Compute native bounds from the grid coordinates (x is "longitude
    # axis" in pixel space, but in projected CRS it's easting metres)
    x_min, x_max = float(cn_grid.x.min()), float(cn_grid.x.max())
    y_min, y_max = float(cn_grid.y.min()), float(cn_grid.y.max())
    try:
        from rasterio.warp import transform_bounds
        west, south, east, north = transform_bounds(
            src_crs, 'EPSG:4326', x_min, y_min, x_max, y_max
        )
    except Exception:
        # Last-resort fallback: assume native is already WGS84
        west, south, east, north = x_min, y_min, x_max, y_max

    # ── Build the colormapped overlay PNG ──────────────────────────────
    # CN values run 30-100. Use RdYlBu_r so high CN (= more runoff) is
    # red, low CN (= more infiltration) is blue. Mask NoData.
    arr = cn_grid.values.astype(float)
    arr = np.ma.masked_invalid(arr)

    # Downsample if huge — keeps overlay PNGs lean for web display
    MAX_OVERLAY_DIM = 2000
    h, w = arr.shape
    if max(h, w) > MAX_OVERLAY_DIM:
        scale = MAX_OVERLAY_DIM / max(h, w)
        new_h, new_w = int(h * scale), int(w * scale)
        try:
            from skimage.transform import resize
            data_ds = resize(arr.filled(np.nan), (new_h, new_w),
                             order=1, anti_aliasing=True, preserve_range=True)
            mask_ds = resize(arr.mask.astype(float), (new_h, new_w),
                             order=0, preserve_range=True) > 0.5
            arr = np.ma.array(data_ds, mask=mask_ds)
        except Exception:
            step_y, step_x = max(1, h // new_h), max(1, w // new_w)
            arr = arr[::step_y, ::step_x]

    cn_min, cn_max = 30.0, 100.0  # NRCS-standard CN range
    norm = mpl_colors.Normalize(vmin=cn_min, vmax=cn_max)
    cmap = plt.get_cmap('RdYlBu_r')
    rgba = cmap(norm(arr))

    # Encode the colourised overlay as a base64 data URI.
    # The VS Code HTML preview renders HTML via `srcdoc` (inline string
    # injection into an iframe), which gives the iframe NO base URL —
    # relative paths like "watershed_cn_overlay.png" simply 404. A data
    # URI is self-contained and works in both srcdoc iframes and regular
    # browser file:// loads. After ≤2000px downsampling the PNG is
    # typically 300 KB–2 MB, well within webview limits.
    import io as _io, base64 as _b64
    _buf = _io.BytesIO()
    plt.imsave(_buf, rgba, format='png')
    _buf.seek(0)
    overlay_data_uri = (
        "data:image/png;base64,"
        + _b64.b64encode(_buf.read()).decode('ascii')
    )
    # Also write the PNG as a sibling artefact for standalone browser use.
    overlay_basename = f"{output_path.stem}_cn_overlay.png"
    overlay_path = output_path.parent / overlay_basename
    plt.imsave(str(overlay_path), rgba)

    # ── Wire the overlay into the Leaflet map via raw <script> ─────────
    # Use `m._id` to get the exact Leaflet variable name folium emits
    # (e.g. `map_a3f9b2…`) rather than scanning Object.values(window),
    # which can miss the map variable in strict VS Code webview sandboxes.
    map_var = f"map_{m._id}"
    overlay_js = f"""
    <script>
    (function() {{
        var _dataUri = {json.dumps(overlay_data_uri)};
        function wireCNOverlay() {{
            if (typeof {map_var} === 'undefined') {{
                return setTimeout(wireCNOverlay, 80);
            }}
            var overlay = L.imageOverlay(
                _dataUri,
                [[{south}, {west}], [{north}, {east}]],
                {{opacity: 0.7, interactive: false}}
            );
            overlay.addTo({map_var});
        }}
        if (document.readyState === 'complete') {{ wireCNOverlay(); }}
        else {{ window.addEventListener('load', wireCNOverlay); }}
    }})();
    </script>
    """
    m.get_root().html.add_child(folium.Element(overlay_js))

    # ── Title + legend ─────────────────────────────────────────────────
    cn_lo = float(np.nanmin(cn_grid.values))
    cn_hi = float(np.nanmax(cn_grid.values))
    cn_mean = float(np.nanmean(cn_grid.values))
    title_html = f"""
    <div style="position: fixed; top: 10px; left: 50px; width: 290px;
                background-color: white; border:2px solid #444;
                z-index:9999; font-size:13px; padding: 10px 12px;
                font-family: system-ui, sans-serif;">
      <b>Curve Number Grid</b><br>
      Gauge ID: {gauge_id}<br>
      Mean CN: {cn_mean:.1f} &middot; Range: {cn_lo:.0f}–{cn_hi:.0f}
    </div>
    """
    m.get_root().html.add_child(folium.Element(title_html))

    legend_html = """
    <div style="position: fixed; bottom: 30px; right: 20px; width: 200px;
                background-color: white; border:2px solid #444; z-index:9999;
                font-size:12px; padding: 10px 12px;
                font-family: system-ui, sans-serif;">
      <b>CN Legend</b><br>
      <div style="height: 14px; margin: 6px 0;
                  background: linear-gradient(to right,
                    #313695, #74add1, #ffffbf, #f46d43, #a50026);"></div>
      <div style="display: flex; justify-content: space-between; font-size: 11px;">
        <span>30 (low runoff)</span><span>100 (high)</span>
      </div>
      <div style="margin-top: 6px; font-size: 11px; color: #555;">
        Palette: RdYlBu_r &middot; NRCS standard
      </div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    folium.LayerControl().add_to(m)
    m.save(str(output_path))
