import os
import re
import pandas as pd
import geopandas as gpd
import numpy as np
from datetime import datetime
import multiprocessing as mp
from config import (
    PARKS_FILE,
    CAPITAL_PROJECTS_FILE,
    CRS,
    CUTOFF_DATE,
    RESOLUTION,
    DATASET_INFO,
    ANALYSIS_BUFFER_FT,
    FEMA_RASTER,
    STORM_RASTER,
    HEAT_FILE,
    HVI_DATA,
    FVI_DATA,
    OUTPUT_GEOJSON
)

# ---------------------------
# Utility Functions
# ---------------------------
def min_max_normalize(series, outlier_percentile=5):
    """Normalize a pandas Series to range [0,1], omitting outliers."""
    s = series.fillna(0).astype(float)
    lower = s.quantile(outlier_percentile / 100)
    upper = s.quantile(1 - outlier_percentile / 100)
    
    if upper - lower == 0:
        return s.apply(lambda x: 0.0 if upper == 0 else 1.0)
    
    return (s.clip(lower=lower, upper=upper) - lower) / (upper - lower)

def compute_index_for_factor_high(gdf, factor_name, config):
    info = config.DATASET_INFO[factor_name]
    raw_col = info["raw"]
    index_col = info["alias"]
    if raw_col not in gdf.columns:
        gdf[raw_col] = 0.0
    gdf[index_col] = min_max_normalize(gdf[raw_col])
    return gdf

def ensure_crs_vector(gdf, target_crs):
    if gdf.crs is None:
        gdf = gdf.set_crs(target_crs)
    elif gdf.crs.to_string() != target_crs:
        gdf = gdf.to_crs(target_crs)
    return gdf

def ensure_crs_raster(raster_path, target_crs, resolution):
    import rasterio
    from rasterio.warp import calculate_default_transform, reproject, Resampling
    from pathlib import Path
    raster_path = Path(raster_path)
    with rasterio.open(raster_path) as src:
        same_crs = (src.crs is not None and src.crs.to_string() == target_crs)
        same_res = np.isclose(src.res[0], resolution, atol=0.1)
        if not same_crs or not same_res:
            print("Reprojecting raster:", raster_path.name)
            transform, width, height = calculate_default_transform(
                src.crs, target_crs, src.width, src.height, *src.bounds, resolution=resolution
            )
            profile = src.meta.copy()
            profile.update({'crs': target_crs, 'transform': transform, 'width': width, 'height': height})
            temp_path = raster_path.parent / f"reprojected_{raster_path.name}"
            with rasterio.open(temp_path, 'w', **profile) as dst:
                for i in range(1, src.count + 1):
                    reproject(
                        source=rasterio.band(src, i),
                        destination=rasterio.band(dst, i),
                        src_transform=src.transform,
                        src_crs=src.crs,
                        dst_transform=transform,
                        dst_crs=target_crs,
                        resampling=Resampling.bilinear
                    )
            return str(temp_path)
        else:
            return str(raster_path)

# ---------------------------
# Capital Projects Processing
# ---------------------------
def reformat_est_investment(value):
    """
    Convert a TotalFundi string value into a numeric value.
    Handles cases such as:
      - "Less than $1 million"
      - "Greater than $10 million"
      - "Between $3 million and $5 million"
      - "$2,972,000"
    """
    if pd.isnull(value):
        return np.nan
    value = value.strip()
    # Handle 'Between $3 million and $5 million'
    between_match = re.match(r"Between \$(\d+(?:\.\d+)?) million and \$(\d+(?:\.\d+)?) million", value, re.IGNORECASE)
    if between_match:
        low = float(between_match.group(1))
        high = float(between_match.group(2))
        mid = (low + high) / 2
        return mid * 1e6
    # Handle 'Less than $1 million' or 'Greater than $10 million'
    lt_gt_match = re.match(r"(Less than|Greater than) \$(\d+(?:\.\d+)?) million", value, re.IGNORECASE)
    if lt_gt_match:
        num = float(lt_gt_match.group(2))
        return num * 1e6
    # Handle standard dollar amounts like "$2,972,000"
    standard_match = re.match(r"\$(.*)", value)
    if standard_match:
        num_str = standard_match.group(1).replace(",", "")
        try:
            return float(num_str)
        except:
            return np.nan
    return np.nan

def process_capital_projects(cap_gdf, config):
    """
    For CapitalProjects:
      - Filter to only 'completed' projects.
      - Drop projects with completion dates (from Construc_4) before the cutoff.
      - Reformat TotalFundi into a numeric EstInvestment.
    """
    cap_gdf = cap_gdf[cap_gdf["CurrentPha"].str.lower() == "completed"].copy()
    cap_gdf["Construc_4_dt"] = pd.to_datetime(cap_gdf["Construc_4"], format="%m/%d/%Y %I:%M:%S %p", errors='coerce')
    cap_gdf = cap_gdf[cap_gdf["Construc_4_dt"] >= config.CUTOFF_DATE].copy()
    cap_gdf["EstInvestment"] = cap_gdf["TotalFundi"].apply(reformat_est_investment)
    return cap_gdf

def allocate_investment_by_tracker(cap_gdf, parks_gdf):
    """
    For CapitalProjects with the same TrackerID (i.e. a multi‐site project),
    allocate the total EstInvestment proportionally to each park based on its 'acres'.
    This is achieved by first performing a spatial join between CapitalProjects and Parks.
    """
    # Spatial join: assign each capital project point the attributes (including acres) from intersecting parks
    cap_joined = gpd.sjoin(cap_gdf, parks_gdf[["globalid", "acres", "geometry"]], how="left", predicate="intersects")
    allocation = []
    for tracker, group in cap_joined.groupby("TrackerID"):
        total_investment = group["EstInvestment"].iloc[0]  # same total for each site
        total_acres = group["acres"].sum()
        for idx, row in group.iterrows():
            prop = row["acres"] / total_acres if total_acres > 0 else 0
            allocated = total_investment * prop
            row["EstInvestment"] = allocated
            allocation.append(row)
    allocated_gdf = gpd.GeoDataFrame(allocation, crs=parks_gdf.crs)
    return allocated_gdf

def aggregate_cap_proj_to_parks(parks_gdf, cap_joined, config):
    """
    For each park polygon, aggregate all intersecting CapitalProjects.
    Concatenate text fields (separated by ", ") and sum the EstInvestment values (stored as EstInvTotal).
    """
    concat_fields = config.DATASET_INFO["CapitalProjects"]["concat_fields"]
    agg_dict = {field: lambda x: ", ".join(x.astype(str)) for field in concat_fields if field != "EstInvestment"}
    agg_dict["EstInvestment"] = "sum"
    cap_agg = cap_joined.groupby("index_right").agg(agg_dict).reset_index()
    cap_agg = cap_agg.rename(columns={"EstInvestment": config.DATASET_INFO["CapitalProjects"]["est_total_field"]})
    parks_gdf = parks_gdf.reset_index().rename(columns={"index": "park_index"})
    merged = parks_gdf.merge(cap_agg, left_on="park_index", right_on="index_right", how="left")
    
    # Replace NaN values in EstInvTotal with 0
    est_total_field = config.DATASET_INFO["CapitalProjects"]["est_total_field"]
    merged[est_total_field] = merged[est_total_field].fillna(0)
    
    return merged

# ---------------------------
# Heat Analysis Functions
# ---------------------------
def kelvin_to_fahrenheit(K):
    return (K - 273.15) * 9/5 + 32

def load_raster_distribution_f(raster_path):
    import rasterio
    with rasterio.open(raster_path) as src:
        data = src.read(1, masked=True)
    data_f = kelvin_to_fahrenheit(data)
    valid = data_f.compressed()
    sorted_values = np.sort(valid)
    return sorted_values

def percentile_from_distribution(value, distribution):
    idx = np.searchsorted(distribution, value, side='right')
    percentile = (idx / len(distribution)) * 100.0
    return percentile

def extract_mean_temperature(site, raster_path):
    import rasterio
    from rasterio.windows import Window
    from shapely.geometry import box
    
    geom = site.geometry
    if geom is None or geom.is_empty:
        return np.nan
    
    # Change: Buffer the polygon directly instead of its centroid
    BUFFER = 2000.0  # using a 2000 ft buffer
    buffer_geom = geom.buffer(BUFFER)
    
    # Get the bounds of the buffered geometry
    xmin, ymin, xmax, ymax = buffer_geom.bounds
    
    with rasterio.open(raster_path) as src:
        # Convert bounds to pixel coordinates
        row_start, col_start = src.index(xmin, ymax)
        row_end, col_end = src.index(xmax, ymin)
        
        # Ensure rows and columns are in the correct order
        row_start, row_end = sorted([row_start, row_end])
        col_start, col_end = sorted([col_start, col_end])
        
        # Ensure coordinates are within raster boundaries
        row_start = max(row_start, 0)
        col_start = max(col_start, 0)
        row_end = min(row_end, src.height - 1)
        col_end = min(col_end, src.width - 1)
        
        if row_end < row_start or col_end < col_start:
            return np.nan
            
        window = Window(col_start, row_start, col_end - col_start + 1, row_end - row_start + 1)
        data = src.read(1, window=window, masked=True)
        
        if data.size == 0:
            return np.nan
            
        data_f = kelvin_to_fahrenheit(data)
        return float(data_f.mean())

def process_site_heat(args):
    site, raster_path = args
    return extract_mean_temperature(site, raster_path)

def compute_raw_heat(gdf, config):
    gdf = ensure_crs_vector(gdf, config.CRS)
    heat_raster_path = ensure_crs_raster(config.HEAT_FILE, config.CRS, config.RESOLUTION)
    sites_list = [(row, heat_raster_path) for idx, row in gdf.iterrows()]
    cpu_cnt = mp.cpu_count()
    with mp.Pool(cpu_cnt - 1) as pool:
        mean_temps = pool.map(process_site_heat, sites_list)
    gdf["heat_mean"] = mean_temps
    distribution = load_raster_distribution_f(heat_raster_path)
    percentiles = [percentile_from_distribution(val, distribution) if np.isfinite(val) else np.nan for val in gdf["heat_mean"]]
    gdf["heat_index"] = [round(1 - (p / 100), 2) if np.isfinite(p) else np.nan for p in percentiles]
    return gdf

def compute_heat_index(gdf, config):
    gdf = compute_raw_heat(gdf, config)
    info = config.DATASET_INFO["Heat_Hazard_Index"]
    gdf[info["raw"]] = gdf["heat_mean"]
    gdf[info["alias"]] = gdf["heat_index"]
    return gdf

# ---------------------------
# Flood Analysis Functions
# ---------------------------
COAST_VALUES = {1: '500', 2: '100'}
STORM_VALUES = {1: 'Shl', 2: 'Dp', 3: 'Tid'}

def read_raster_window(raster_path, bbox, target_crs):
    import rasterio
    with rasterio.open(raster_path) as src:
        if src.crs is not None and src.crs.to_string() != target_crs:
            raise ValueError(f"Raster {raster_path} CRS ({src.crs}) does not match {target_crs}.")
        window = src.window(*bbox)
        data = src.read(1, window=window, masked=False)
        transform = src.window_transform(window)
        return data, transform

def process_site_flood(args):
    idx, site, fema_path, storm_path, buffer_dist, target_crs = args
    from shapely.geometry import box
    geom = site.geometry
    if geom is None or geom.is_empty:
        return idx, {
            'Cst_500_nr': 0.0, 'Cst_100_nr': 0.0,
            'StrmShl_nr': 0.0, 'StrmDp_nr': 0.0, 'StrmTid_nr': 0.0
        }
    # Change: Buffer the polygon directly instead of its centroid
    buffer_geom = geom.buffer(buffer_dist)
    minx, miny, maxx, maxy = buffer_geom.bounds
    bbox = (minx, miny, maxx, maxy)
    try:
        fema_arr, fema_transform = read_raster_window(fema_path, bbox, target_crs)
        storm_arr, _ = read_raster_window(storm_path, bbox, target_crs)
    except Exception as e:
        return idx, {
            'Cst_500_nr': 0.0, 'Cst_100_nr': 0.0,
            'StrmShl_nr': 0.0, 'StrmDp_nr': 0.0, 'StrmTid_nr': 0.0
        }
    min_height = min(fema_arr.shape[0], storm_arr.shape[0])
    min_width = min(fema_arr.shape[1], storm_arr.shape[1])
    fema_arr = fema_arr[:min_height, :min_width]
    storm_arr = storm_arr[:min_height, :min_width]
    height, width = fema_arr.shape
    from rasterio import features
    site_rast = features.rasterize([(geom, 1)], out_shape=(height, width),
                                    transform=fema_transform, fill=0, dtype=np.uint8)
    buffer_rast = features.rasterize([(buffer_geom, 1)], out_shape=(height, width),
                                      transform=fema_transform, fill=0, dtype=np.uint8)
    buffer_mask = (buffer_rast == 1)
    results = {}
    for cval, ctag in COAST_VALUES.items():
        nr_match = ((buffer_mask) & (fema_arr == cval)).sum() if buffer_mask.sum() > 0 else 0
        results[f"Cst_{ctag}_nr"] = nr_match / buffer_mask.sum() if buffer_mask.sum() else 0.0
    for sval, stag in STORM_VALUES.items():
        nr_match = ((buffer_mask) & (storm_arr == sval)).sum() if buffer_mask.sum() > 0 else 0
        results[f"Strm{stag}_nr"] = nr_match / buffer_mask.sum() if buffer_mask.sum() else 0.0
    return idx, results

def compute_raw_flood(gdf, config):
    gdf = ensure_crs_vector(gdf, config.CRS)
    buffer_dist = config.ANALYSIS_BUFFER_FT
    fema_raster = config.FEMA_RASTER
    storm_raster = config.STORM_RASTER
    args_list = [
        (idx, row, fema_raster, storm_raster, buffer_dist, config.CRS)
        for idx, row in gdf.iterrows()
    ]
    from concurrent.futures import ProcessPoolExecutor
    cpu_cnt = max(1, mp.cpu_count() - 1)
    with ProcessPoolExecutor(max_workers=cpu_cnt) as executor:
        results = list(executor.map(process_site_flood, args_list))
    results_dict = {idx: res for idx, res in results}
    results_df = pd.DataFrame.from_dict(results_dict, orient='index')
    flood_components = ['Cst_500_nr', 'Cst_100_nr', 'StrmShl_nr', 'StrmDp_nr', 'StrmTid_nr']
    gdf = gdf.drop(columns=flood_components, errors='ignore')
    gdf = gdf.join(results_df[flood_components])
    return gdf

def compute_flood_hazard_indices(gdf, config, coastal_weights=None, stormwater_weights=None):
    flood_components = ['Cst_500_nr', 'Cst_100_nr', 'StrmShl_nr', 'StrmDp_nr', 'StrmTid_nr']
    if not all(comp in gdf.columns for comp in flood_components):
        gdf = compute_raw_flood(gdf, config)
    if coastal_weights is None:
        coastal_weights = {'Cst_500_nr': 0.15, 'Cst_100_nr': 0.35, 'StrmTid_nr': 0.5}
    coastal_raw = (coastal_weights['Cst_500_nr'] * gdf['Cst_500_nr'] +
                   coastal_weights['Cst_100_nr'] * gdf['Cst_100_nr'] +
                   coastal_weights['StrmTid_nr'] * gdf['StrmTid_nr'])
    coastal_raw_field = config.DATASET_INFO["Coastal_Flood_Hazard_Index"]["raw"]
    gdf[coastal_raw_field] = coastal_raw
    coastal_alias = config.DATASET_INFO["Coastal_Flood_Hazard_Index"]["alias"]
    gdf[coastal_alias] = 1 - coastal_raw
    if stormwater_weights is None:
        stormwater_weights = {'StrmShl_nr': 0.3, 'StrmDp_nr': 0.7}
    storm_raw = (stormwater_weights['StrmShl_nr'] * gdf['StrmShl_nr'] +
                 stormwater_weights['StrmDp_nr'] * gdf['StrmDp_nr'])
    storm_raw_field = config.DATASET_INFO["Stormwater_Flood_Hazard_Index"]["raw"]
    gdf[storm_raw_field] = storm_raw
    storm_alias = config.DATASET_INFO["Stormwater_Flood_Hazard_Index"]["alias"]
    gdf[storm_alias] = 1 - storm_raw
    return gdf

# ---------------------------
# Vulnerability Analysis Functions
# ---------------------------
def area_weighted_average(buffer_geom, features_gdf, field_name):
    total_area = 0.0
    weighted_sum = 0.0
    for idx, row in features_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        intersection = buffer_geom.intersection(geom)
        if not intersection.is_empty:
            area = intersection.area
            try:
                value = float(row.get(field_name, 0))
            except (ValueError, TypeError):
                continue
            weighted_sum += value * area
            total_area += area
    return weighted_sum / total_area if total_area > 0 else float('nan')

def compute_raw_heat_vulnerability(gdf, config):
    gdf = ensure_crs_vector(gdf, config.CRS)
    buffer_dist = config.ANALYSIS_BUFFER_FT
    hvi = gpd.read_file(config.HVI_DATA)
    hvi = ensure_crs_vector(hvi, config.CRS)
    hvi_sindex = hvi.sindex
    hvi_values = []
    for idx, site in gdf.iterrows():
        geom = site.geometry
        if geom is None or geom.is_empty:
            hvi_values.append(np.nan)
            continue
        buffer_geom = geom.buffer(buffer_dist)
        possible_hvi = hvi.iloc[list(hvi_sindex.intersection(buffer_geom.bounds))]
        hvi_val = area_weighted_average(buffer_geom, possible_hvi, "HVI")
        hvi_values.append(hvi_val)
    raw_field = config.DATASET_INFO["Heat_Vulnerability_Index"].get("raw", "hvi_area")
    gdf[raw_field] = hvi_values
    return gdf

def compute_heat_vulnerability_index(gdf, config):
    raw_field = config.DATASET_INFO["Heat_Vulnerability_Index"].get("raw", "hvi_area")
    if raw_field not in gdf.columns:
        gdf = compute_raw_heat_vulnerability(gdf, config)
    gdf = compute_index_for_factor_high(gdf, "Heat_Vulnerability_Index", config)
    return gdf

def compute_raw_flood_vulnerability(gdf, config):
    gdf = ensure_crs_vector(gdf, config.CRS)
    buffer_dist = config.ANALYSIS_BUFFER_FT
    fvi = gpd.read_file(config.FVI_DATA)
    fvi = ensure_crs_vector(fvi, config.CRS)
    fvi_sindex = fvi.sindex
    ss80_values = []
    tid80_values = []
    for idx, site in gdf.iterrows():
        geom = site.geometry
        if geom is None or geom.is_empty:
            ss80_values.append(np.nan)
            tid80_values.append(np.nan)
            continue
        buffer_geom = geom.buffer(buffer_dist)
        possible_fvi = fvi.iloc[list(fvi_sindex.intersection(buffer_geom.bounds))]
        ss80_val = area_weighted_average(buffer_geom, possible_fvi, "ss_80s")
        tid80_val = area_weighted_average(buffer_geom, possible_fvi, "tid_80s")
        ss80_values.append(ss80_val)
        tid80_values.append(tid80_val)
    gdf["ssvul_area"] = ss80_values
    gdf["tivul_area"] = tid80_values
    flood_raw_field = config.DATASET_INFO["Flood_Vulnerability_Index"].get("raw", "flood_vuln")
    gdf[flood_raw_field] = gdf[["ssvul_area", "tivul_area"]].mean(axis=1)
    return gdf

def compute_flood_vulnerability_index(gdf, config):
    if "flood_vuln" not in gdf.columns:
        gdf = compute_raw_flood_vulnerability(gdf, config)
    gdf = compute_index_for_factor_high(gdf, "Flood_Vulnerability_Index", config)
    return gdf

# ---------------------------
# Main Analysis Execution
# ---------------------------
def run_analysis():
    # Load Parks and CapitalProjects datasets
    parks = gpd.read_file(PARKS_FILE)
    parks = ensure_crs_vector(parks, CRS)
    cap_projects = gpd.read_file(CAPITAL_PROJECTS_FILE)
    cap_projects = ensure_crs_vector(cap_projects, CRS)
    
    # Process CapitalProjects: filter and reformat funding values
    import config  # import config to pass as module
    cap_projects = process_capital_projects(cap_projects, config)
    
    # Allocate multi-site project funding using park acres
    cap_projects_alloc = allocate_investment_by_tracker(cap_projects, parks)
    
    # Spatial join (intersection)
    if "index_right" in cap_projects_alloc.columns:
        cap_projects_alloc = cap_projects_alloc.drop(columns=["index_right"])
    cap_joined = gpd.sjoin(cap_projects_alloc, parks[['acres', 'globalid', 'geometry']], how="left", predicate="intersects")
    
    # Aggregate capital project fields to each park
    parks_agg = aggregate_cap_proj_to_parks(parks, cap_joined, config)
    
    # Compute raster‐based indices on parks_agg
    parks_agg = compute_heat_index(parks_agg, config)
    parks_agg = compute_flood_hazard_indices(parks_agg, config)
    parks_agg = compute_heat_vulnerability_index(parks_agg, config)
    parks_agg = compute_flood_vulnerability_index(parks_agg, config)
    
    # --- NEW INDEX CALCULATIONS ---
    # Normalize total investment
    parks_agg["Inv_Norm"] = min_max_normalize(parks_agg["EstInvTotal"])
    
    # Compute hazard_factor (weighted: 25% Coastal, 50% Stormwater, 25% Heat)
    hf_w = config.HAZARD_FACTOR_WEIGHTS
    parks_agg["hazard_factor"] = (
         hf_w["CoastalFloodHaz"] * parks_agg[config.DATASET_INFO["Coastal_Flood_Hazard_Index"]["alias"]] +
         hf_w["StormFloodHaz"] * parks_agg[config.DATASET_INFO["Stormwater_Flood_Hazard_Index"]["alias"]] +
         hf_w["HeatHaz"] * parks_agg[config.DATASET_INFO["Heat_Hazard_Index"]["alias"]]
    )
    
    # Compute vul_factor (weighted: 50% Heat Vulnerability, 50% Flood Vulnerability)
    vf_w = config.VULNERABILITY_FACTOR_WEIGHTS
    parks_agg["vul_factor"] = (
         vf_w["HeatVuln"] * parks_agg[config.DATASET_INFO["Heat_Vulnerability_Index"]["alias"]] +
         vf_w["FloodVuln"] * parks_agg[config.DATASET_INFO["Flood_Vulnerability_Index"]["alias"]]
    )
    
    # Compute suitability (weighted: 25% hazard, 25% vulnerability, 50% investment inverted)
    su_w = config.SUITABILITY_WEIGHTS
    parks_agg["suitability"] = (
         su_w["hazard_factor"] * parks_agg["hazard_factor"] +
         su_w["vul_factor"] * parks_agg["vul_factor"] +
         su_w["Inv_Norm"] * (1 - parks_agg["Inv_Norm"])
    )
    
    # Write the final geojson to output
    parks_agg.to_file(OUTPUT_GEOJSON, driver="GeoJSON")
    print("Analysis complete. Output saved to:", OUTPUT_GEOJSON)