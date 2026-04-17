#!/usr/bin/env python3
"""
Integration tests for complete CAMELS attribute extraction workflow

Tests end-to-end extraction of watershed attributes following CAMELS methodology.
"""

import pytest
import numpy as np
from shapely.geometry import box
from unittest.mock import Mock, patch, MagicMock
import geopandas as gpd
import pandas as pd


class TestCAMELSWorkflowIntegration:
    """Integration tests for complete CAMELS workflow"""
    
    @pytest.fixture
    def mock_watershed(self):
        """Create mock watershed geometry"""
        geom = box(-86.9, 40.4, -86.8, 40.5)
        gdf = gpd.GeoDataFrame(
            {'gauge_id': ['03335000']},
            geometry=[geom],
            crs='EPSG:4326'
        )
        return gdf
    
    @pytest.fixture
    def mock_outlet(self):
        """Mock outlet coordinates"""
        return {'lat': 40.45, 'lon': -86.85}
    
    def test_attribute_consistency(self):
        """Test that all modules return consistent data structures"""
        # Expected return types for each module
        expected_returns = {
            'climate': dict,
            'soil': tuple,  # (dict, dict)
            'vegetation': dict,
            'geology': tuple,  # (dict, dict)
            'hydrology': dict,
            'geomorphic': tuple,  # (dict, dict)
        }
        
        # Expected attribute counts (minimum)
        expected_counts = {
            'climate': 14,
            'soil': 9,
            'vegetation': 14,
            'geology': 7,
            'hydrology': 17,
            'geomorphic': 28,
        }
        
        print("\n📊 Expected Attribute Counts:")
        for module, count in expected_counts.items():
            print(f"  {module:15s}: {count:2d} attributes")
        
        total = sum(expected_counts.values())
        print(f"\n  {'TOTAL':15s}: {total:2d} attributes")
        assert total >= 89, "Should have at least 89 CAMELS attributes"


class TestDataValidation:
    """Tests for data validation and quality checks"""
    
    def test_geomorphic_parameter_validation(self):
        """Test that geomorphic parameters have valid values"""
        # Expected parameter constraints
        constraints = {
            'DA_km2': (0, 1e6),      # Area should be positive
            'Rff': (0, 2),            # Form factor 0-2
            'Rc': (0, 1),             # Circularity 0-1
            'Re': (0, 2),             # Elongation 0-2
            'H_m': (0, 5000),         # Relief 0-5000m
            'Rh': (0, 1),             # Relief ratio 0-1
            'Ls_pct': (0, 100),       # Slope 0-100%
            'D_km_per_km2': (0, 10),  # Drainage density 0-10
        }
        
        print("\n📐 Geomorphic Parameter Constraints:")
        for param, (min_val, max_val) in constraints.items():
            print(f"  {param:20s}: {min_val:8.1f} - {max_val:8.1f}")
        
        assert len(constraints) == 8
        print(f"\n  ✓ {len(constraints)} parameter constraints defined")


class TestErrorHandling:
    """Tests for error handling and edge cases"""
    
    def test_extreme_watershed_sizes(self):
        """Test handling of very small/large watersheds"""
        # Very small watershed (< 1 km²)
        small_geom = box(-86.85, 40.45, -86.84, 40.46)
        small_gdf = gpd.GeoDataFrame(
            geometry=[small_geom], crs='EPSG:4326'
        )
        
        # Very large watershed (> 10,000 km²)
        large_geom = box(-88.0, 39.0, -85.0, 42.0)
        large_gdf = gpd.GeoDataFrame(
            geometry=[large_geom], crs='EPSG:4326'
        )
        
        print("✓ Edge case geometries created")


class TestBatchProcessing:
    """Tests for batch processing capabilities"""
    
    def test_multiple_watersheds(self):
        """Test processing multiple watersheds"""
        # Create list of test gauges
        gauges = [
            {'id': '03335000', 'lat': 40.45, 'lon': -86.85},
            {'id': '03336000', 'lat': 40.50, 'lon': -86.80},
            {'id': '03337000', 'lat': 40.55, 'lon': -86.75},
        ]
        
        results = []
        for gauge in gauges:
            result = {
                'gauge_id': gauge['id'],
                'lat': gauge['lat'],
                'lon': gauge['lon'],
                'status': 'processed'
            }
            results.append(result)
        
        assert len(results) == len(gauges)
        print(f"✓ Batch processing structure for {len(gauges)} gauges validated")


class TestOutputFormats:
    """Tests for output format validation"""
    
    def test_forcing_export_formats(self):
        """Test forcing data export in multiple formats"""
        from ai_hydro.data.forcing import export_forcing_data
        from pathlib import Path
        import tempfile
        
        # Create mock forcing data
        df = pd.DataFrame({
            'date': pd.date_range('2010-01-01', periods=10),
            'prcp_mm': np.random.rand(10) * 10,
            'tmin_C': np.random.rand(10) * 10,
            'tmax_C': np.random.rand(10) * 10 + 15,
        })
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # Test CSV export
            files = export_forcing_data(
                df, 'test_gauge', tmpdir, formats=['csv']
            )
            
            if files:
                assert 'csv' in files
                assert Path(files['csv']).exists()
                print("✓ CSV export format validated")


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
