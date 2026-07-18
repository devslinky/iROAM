""" unit tests for each stage of offline_debug.py using frozen fixture in tests/fixtures/offline_debug/"""

from datetime import date

import pandas as pd
import pytest
from pathlib import Path as PATH
from scripts.analysis.offline_debug import (setup, _load_static, _stage_extract, _stage_project, _stage_upsample, _stage_group, _stage_label)

def test_stage_extract_parity():
    """Test that _stage_extract produces the same output as the frozen golden output."""

    route = "29"
    service_date = date.fromisoformat("2026-06-18")
    trip_id = "10020"
    
    sd = pd.to_datetime(service_date).date()
    st = sd.strftime("%Y%m%d")

    ctx = setup()

    # assert that we are in offline mode 
    assert ctx.OFFLINE == True
    assert ctx.DATA_MODE == "cache"

    # set the cache and manifest paths to the frozen fixture
    ctx.CACHE = PATH("tests/fixtures/offline")
    ctx.MANIFEST = PATH("tests/fixtures/offline/manifest.json")

    # explicitly load the static data to ensure it is available for the stage
    _load_static(ctx)

    df = _stage_extract(trip_id, st, ctx)
    expected_df = pd.read_parquet("tests/fixtures/offline/extract_10020_20260618_golden.parquet")
    pd.testing.assert_frame_equal(df, expected_df, check_exact=False, rtol=1e-5, check_dtype=False)

    # randomly change a value in the df to ensure that the test fails if the output is different from the golden output
    # change vehicle_id to 10 of the first row
    df_error = df.copy()
    df_error.loc[0, "vehicle_id"] = 10

    with pytest.raises(AssertionError):
        pd.testing.assert_frame_equal(df_error, expected_df, check_exact=False, rtol=1e-5, check_dtype=False)



def test_stage_project_parity():
    """Test that _stage_project produces the same output as the frozen golden output."""
    
    route = "29"
    service_date = date.fromisoformat("2026-06-18")
    trip_id = "10020"
    
    sd = pd.to_datetime(service_date).date()
    st = sd.strftime("%Y%m%d")

    ctx = setup()

    # assert that we are in offline mode 
    assert ctx.OFFLINE == True
    assert ctx.DATA_MODE == "cache"

    # set the cache and manifest paths to the frozen fixture
    ctx.CACHE = PATH("tests/fixtures/offline")
    ctx.MANIFEST = PATH("tests/fixtures/offline/manifest.json")

    # explicitly load the static data to ensure it is available for the stage
    _load_static(ctx)

    df_projected = _stage_project(trip_id, st, ctx)
    
    expected_df_projected = pd.read_parquet("tests/fixtures/offline/project_10020_20260618_golden.parquet")
    
    pd.testing.assert_frame_equal(df_projected, expected_df_projected, check_exact=False, rtol=1e-5, check_dtype=False)

    # randomly change a value in the df to ensure that the test fails if the output is different from the golden output
    # change vehicle_id to 10 of the first row
    df_error = df_projected.copy()
    df_error.loc[0, "vehicle_id"] = 10

    with pytest.raises(AssertionError):
        pd.testing.assert_frame_equal(df_error, expected_df_projected, check_exact=False, rtol=1e-5, check_dtype=False)


def test_stage_upsample_parity():
    """Test that _stage_upsample produces the same output as the frozen golden output."""
    
    route = "29"
    service_date = date.fromisoformat("2026-06-18")
    trip_id = "10020"
    
    sd = pd.to_datetime(service_date).date()
    st = sd.strftime("%Y%m%d")

    ctx = setup()

    # assert that we are in offline mode 
    assert ctx.OFFLINE == True
    assert ctx.DATA_MODE == "cache"

    # set the cache and manifest paths to the frozen fixture
    ctx.CACHE = PATH("tests/fixtures/offline")
    ctx.MANIFEST = PATH("tests/fixtures/offline/manifest.json")

    # explicitly load the static data to ensure it is available for the stage
    _load_static(ctx)

    df_upsampled = _stage_upsample(trip_id, st, ctx)
    
    expected_df_upsampled = pd.read_parquet("tests/fixtures/offline/upsample_10020_20260618_golden.parquet")
    
    pd.testing.assert_frame_equal(df_upsampled, expected_df_upsampled, check_exact=False, rtol=1e-5, check_dtype=False)

    # randomly change a value in the df to ensure that the test fails if the output is different from the golden output
    # change vehicle_id to 10 of the first row
    df_error = df_upsampled.copy()
    df_error.loc[0, "vehicle_id"] = 10

    with pytest.raises(AssertionError):
        pd.testing.assert_frame_equal(df_error, expected_df_upsampled, check_exact=False, rtol=1e-5, check_dtype=False)


def test_stage_group_parity():
    """Test that _stage_group produces the same output as the frozen golden output."""
    
    route = "29"
    service_date = date.fromisoformat("2026-06-18")
    direction = int(0) 
    trip_id = "10020" # trip 10020 is actually direction_id = 1 but the golden output was generated with direction_id = 0
    
    sd = pd.to_datetime(service_date).date()
    st = sd.strftime("%Y%m%d")

    ctx = setup()

    # assert that we are in offline mode 
    assert ctx.OFFLINE == True
    assert ctx.DATA_MODE == "cache"

    # set the cache and manifest paths to the frozen fixture
    ctx.CACHE = PATH("tests/fixtures/offline")
    ctx.MANIFEST = PATH("tests/fixtures/offline/manifest.json")

    # explicitly load the static data to ensure it is available for the stage
    _load_static(ctx)

    df_grouped = _stage_group(sd, route, direction, ctx, view_buses_as_flat_df=True)
    
    expected_df_grouped = pd.read_parquet("tests/fixtures/offline/group_29_20260618_golden.parquet")
    
    pd.testing.assert_frame_equal(df_grouped, expected_df_grouped, check_exact=False, rtol=1e-5, check_dtype=False)

    # randomly change a value in the df to ensure that the test fails if the output is different from the golden output
    # change vehicle_id to 10 of the first row
    df_error = df_grouped.copy()
    df_error.loc[0, "vehicle_id"] = 10

    with pytest.raises(AssertionError):
        pd.testing.assert_frame_equal(df_error, expected_df_grouped, check_exact=False, rtol=1e-5, check_dtype=False)


def test_stage_label_parity():
    """Test that _stage_label produces the same output as the frozen golden output."""
    
    route = "29"
    service_date = date.fromisoformat("2026-06-18")
    direction = int(0) 
    trip_id = "10020" # trip 10020 is actually direction_id = 1 but the golden output was generated with direction_id = 0
    
    sd = pd.to_datetime(service_date).date()
    st = sd.strftime("%Y%m%d")

    ctx = setup()

    # assert that we are in offline mode 
    assert ctx.OFFLINE == True
    assert ctx.DATA_MODE == "cache"

    # set the cache and manifest paths to the frozen fixture
    ctx.CACHE = PATH("tests/fixtures/offline")
    ctx.MANIFEST = PATH("tests/fixtures/offline/manifest.json")

    # explicitly load the static data to ensure it is available for the stage
    _load_static(ctx)

    df_labelled_examples = _stage_label(sd, route, direction, ctx, view_examples_as_df=True)
    
    expected_df_labelled_examples = pd.read_parquet("tests/fixtures/offline/label_29_20260618_golden.parquet")
    
    pd.testing.assert_frame_equal(df_labelled_examples, expected_df_labelled_examples, check_exact=False, rtol=1e-5, check_dtype=False)

    # randomly change a value in the df to ensure that the test fails if the output is different from the golden output
    # change vehicle_id to 10 of the first row
    df_error = df_labelled_examples.copy()
    df_error.loc[0, "vehicle_id"] = 10 

    with pytest.raises(AssertionError):
        pd.testing.assert_frame_equal(df_error, expected_df_labelled_examples, check_exact=False, rtol=1e-5, check_dtype=False)