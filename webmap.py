import os
import re
import json
import folium
import geopandas as gpd
from datetime import datetime
from pathlib import Path

import rasterio
from rasterio import warp
from rasterio.enums import Resampling
import jenkspy

from config import (
    cutoff_date_simple,
    DATASET_INFO,
    ICONS_DIR,
    INDEX_ICONS,
    OUTLINE_SVG,
    OUTPUT_DIR,
    OUTPUT_GEOJSON,
    OUTPUT_WEBMAP,
    HEAT_FILE,
    FEMA_RASTER,
    STORM_RASTER,
    FVI_DATA,
    HVI_DATA,
    RESOLUTION,
    LEGEND_STYLES
)

###############################################################################
# 1. CSS STYLE BLOCK
###############################################################################
STYLE_BLOCK = """
<style>
  /* Overall popup styling */
  .popup-content {
    font-family: sans-serif;
    line-height: 1.4em;
    color: #333;
  }
  
  .popup-header {
    font-size: 20px;
    font-weight: bold;
    margin-bottom: 0.4em;
  }
  
  /* Bubbles (cards) */
  .info-bubble {
    background: #f9f9f9;
    border-radius: 8px;
    padding: 10px;
    margin-bottom: 10px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
  }
  .info-bubble h4 {
    margin-top: 0;
    margin-bottom: 0.6em;
    font-size: 16px;
    font-weight: bold;
  }
  
  /* Collapsible capital projects link */
  .collapsible {
    margin-top: 10px;
    border: 1px solid #ccc;
    border-radius: 8px;
    background-color: #eaeaea;
    overflow: hidden;
  }
  .collapsible summary {
    padding: 8px;
    margin: 0;
    font-size: 14px;
    cursor: pointer;
  }
  
  /* Collapsible table styling */
  .popup-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  .popup-table th, .popup-table td {
    border: 1px solid #ddd;
    padding: 4px;
    text-align: center;
  }
  .scrollable-table {
    max-height: 150px;
    overflow-y: auto;
  }
  
  /* Icon rows & columns */
  .icon-row {
    display: flex;
    justify-content: space-evenly; 
    align-items: center; 
    gap: 10px;
  }
  .icon-col {
    display: flex;
    flex-direction: column;
    align-items: center;
  }

  /* Container for the icon and outline */
  .circle-bg {
    position: relative;
    width: 60px;
    height: 60px;
    background: none;
  }
  .circle-icon {
    width: 60px;
    height: 60px;
  }
  .icon-outline {
    width: 60px;
    height: 60px;
    position: absolute;
    top: 0;
    left: 0;
    opacity: 1;
  }

  .icon-label {
    font-size: 12px;
    margin-top: 4px;
    color: #333;
  }
</style>
"""

###############################################################################
# 2. HELPER FUNCTIONS FOR COLOR & RASTER PROCESSING
###############################################################################
def hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

def rgb_to_hex(rgb):
    return '#{:02x}{:02x}{:02x}'.format(*rgb)

def interpolate_color(val, start_hex, end_hex):
    import numpy as np
    if np.isnan(val):
        return start_hex
    start_rgb = hex_to_rgb(start_hex)
    end_rgb = hex_to_rgb(end_hex)
    val = max(0, min(1, val))
    interp = tuple(int(s + (e - s) * val) for s, e in zip(start_rgb, end_rgb))
    return rgb_to_hex(interp)

def hex_to_rgba(hex_color):
    """Convert hex color to RGBA tuple, handling 8-digit hex with alpha."""
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 8:  # RRGGBBAA format
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4, 6))
    elif len(hex_color) == 6:  # RRGGBB format
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4)) + (255,)
    else:
        raise ValueError(f"Invalid hex color: {hex_color}")

def interpolate_color_with_alpha(val, start_hex, end_hex):
    """Interpolate between two colors, including alpha if present."""
    import numpy as np
    if np.isnan(val):
        return start_hex
    
    start_rgba = hex_to_rgba(start_hex)
    end_rgba = hex_to_rgba(end_hex)
    
    val = max(0, min(1, val))
    interp = tuple(int(s + (e - s) * val) for s, e in zip(start_rgba, end_rgba))
    
    # Convert back to hex with alpha if applicable
    if len(interp) == 4:
        return '#{:02x}{:02x}{:02x}{:02x}'.format(*interp)
    else:
        return '#{:02x}{:02x}{:02x}'.format(*interp[:3])

def process_raster_for_web(input_raster, output_png, target_crs="EPSG:4326", colormap="heat"):
    """
    Reprojects, downsamples, and applies a colormap to a raster,
    then saves it as a PNG for use as an ImageOverlay.
    """
    from affine import Affine
    from PIL import Image
    import numpy as np
    import traceback
    import rasterio
    from rasterio import warp
    from rasterio.enums import Resampling

    try:
        # Verify the input file exists
        if not os.path.exists(input_raster):
            print(f"ERROR: Input raster file does not exist: {input_raster}")
            raise FileNotFoundError(f"Input file not found: {input_raster}")
        
        # Reproject the raster
        with rasterio.open(input_raster) as src:
            # Store original bounds for accurate placement
            src_bounds = src.bounds
            
            # Get reprojected bounds for exact positioning
            dst_bounds = warp.transform_bounds(src.crs, target_crs, *src.bounds)
            
            print(f"Processing {input_raster}")
            print(f"  Original bounds (source CRS): {src_bounds}")
            print(f"  Transformed bounds (target CRS): {dst_bounds}")
            print(f"  Using resolution: {RESOLUTION} feet")
            
            # Verify the raster has valid bounds
            if not all(np.isfinite(b) for b in dst_bounds):
                print(f"ERROR: Invalid bounds after reprojection: {dst_bounds}")
                print("Attempting to use original bounds as fallback")
                try:
                    dst_bounds = src_bounds
                    if not all(np.isfinite(b) for b in dst_bounds):
                        raise ValueError("Source bounds are also invalid")
                except:
                    print("Failed to use original bounds. Using NYC extent as fallback.")
                    dst_bounds = (-74.26, 40.49, -73.69, 40.91)
            
            # Calculate dimensions at specified resolution
            try:
                if target_crs == "EPSG:4326":
                    # At 40°N latitude: 1 degree ≈ 69 miles ≈ 364,320 feet
                    # For EPSG:4326, calculate approximate feet per degree
                    feet_per_degree_lng = 364320 * np.cos(np.radians(40.7))
                    feet_per_degree_lat = 364320

                    width_feet = (dst_bounds[2] - dst_bounds[0]) * feet_per_degree_lng
                    height_feet = (dst_bounds[3] - dst_bounds[1]) * feet_per_degree_lat

                    width = max(100, int(width_feet / RESOLUTION))
                    height = max(100, int(height_feet / RESOLUTION))
                else:
                    width = max(100, int((dst_bounds[2] - dst_bounds[0]) / RESOLUTION))
                    height = max(100, int((dst_bounds[3] - dst_bounds[1]) / RESOLUTION))
                
                print(f"  Target dimensions: {width}x{height} pixels")
                
                # Limit maximum size for memory reasons
                max_size = 2000
                if width > max_size or height > max_size:
                    scale = min(max_size / width, max_size / height)
                    width = int(width * scale)
                    height = int(height * scale)
                    print(f"  Scaled dimensions: {width}x{height} pixels")
            except Exception as e:
                print(f"Error calculating dimensions: {e}")
                traceback.print_exc()
                width, height = 1000, 1000
                print(f"  Using fallback dimensions: {width}x{height} pixels")
            
            # Calculate reprojection transform
            transform = Affine.translation(dst_bounds[0], dst_bounds[3]) * Affine.scale(
                (dst_bounds[2] - dst_bounds[0]) / width, (dst_bounds[1] - dst_bounds[3]) / height
            )
            
            kwargs = src.meta.copy()
            kwargs.update({
                'crs': target_crs,
                'transform': transform,
                'width': width,
                'height': height
            })
            
            data = np.empty((src.count, height, width), dtype=src.dtypes[0])
            
            try:
                for i in range(1, src.count + 1):
                    warp.reproject(
                        source=rasterio.band(src, i),
                        destination=data[i-1],
                        src_transform=src.transform,
                        src_crs=src.crs,
                        dst_transform=transform,
                        dst_crs=target_crs,
                        resampling=Resampling.bilinear
                    )
                nodata = src.nodata
            except Exception as e:
                print(f"Error during reprojection: {e}")
                traceback.print_exc()
                data = np.zeros((src.count, height, width), dtype=np.float32)
                nodata = None

        # Use the first band for visualization
        band = data[0]
        
        # Handle nodata values
        if nodata is not None:
            valid = (band != nodata) & (~np.isnan(band))
        else:
            valid = ~np.isnan(band)
        
        # Debug: Print out the range of valid data values
        valid_data = band[valid]
        
        # Compute normalization range:
        # For heat rasters, use 75th to 100th percentiles (i.e. the top 25% of values)
        # Otherwise (e.g., for continuous flood data), use 5th to 95th percentiles.
        if valid_data.size > 0:
            if colormap == "heat":
                lower = np.percentile(valid_data, 75)
                upper = np.percentile(valid_data, 100)
            else:
                lower = np.percentile(valid_data, 5)
                upper = np.percentile(valid_data, 95)
            min_val = lower if lower < upper else valid_data.min()
            max_val = upper if upper > lower else valid_data.max()
        else:
            min_val = 0
            max_val = 1
        
        # Create an empty RGBA array
        rgba = np.zeros((height, width, 4), dtype=np.uint8)
        
        # Select colormap parameters based on the colormap argument
        if colormap == "heat":
            ramp = DATASET_INFO["Webmap"]["Summer_Temperature"]["color_ramp"]
            start_hex = ramp["start"]  # e.g., "#C40A0A00" (transparent)
            end_hex = ramp["end"]      # e.g., "#C40A0A" (solid)
            print(f"  Heat colormap: {start_hex} to {end_hex}")
            
            # Check if the start color includes an alpha component
            has_transparency = len(start_hex.lstrip('#')) == 8
            print(f"  Using transparency gradient: {has_transparency}")
            
            layer_key = "Summer_Temperature"
            
        elif colormap == "flood":
            if "FEMA" in input_raster:
                start_hex = DATASET_INFO["Webmap"]["FEMA_FloodHaz"]["hex_0_2pct"]
                end_hex = DATASET_INFO["Webmap"]["FEMA_FloodHaz"]["hex_1pct"]
                print(f"  FEMA flood colormap: {start_hex} to {end_hex}")
                layer_key = "FEMA_FloodHaz"
            else:
                # For stormwater, use the colors from the 2080_Stormwater config entry.
                # The value 1 corresponds to shallow (lighter) and 2 to deep (darker).
                ramp = DATASET_INFO["Webmap"]["2080_Stormwater"]["color_ramp"]
                start_hex = ramp["start"]
                end_hex = ramp["end"]
                print(f"  Storm flood colormap: {start_hex} to {end_hex}")
                layer_key = "2080_Stormwater"
        else:
            start_hex = "#FFFFFF"
            end_hex = "#000000"
            print(f"  Default colormap: {start_hex} to {end_hex}")
            layer_key = None
        
        # Special processing for FEMA flood data
        if "FEMA" in input_raster:
            for i in range(height):
                for j in range(width):
                    if not valid[i, j] or band[i, j] == 0:
                        rgba[i, j] = [0, 0, 0, 0]  # Fully transparent
                    elif band[i, j] == 1:  # 100-year flood (1%)
                        r, g, b = hex_to_rgb(DATASET_INFO["Webmap"]["FEMA_FloodHaz"]["hex_1pct"])
                        alpha = int(DATASET_INFO["Webmap"]["FEMA_FloodHaz"].get("opacity", 0.6) * 255)
                        rgba[i, j] = [r, g, b, alpha]
                    elif band[i, j] == 2:  # 500-year flood (0.2%)
                        r, g, b = hex_to_rgb(DATASET_INFO["Webmap"]["FEMA_FloodHaz"]["hex_0_2pct"])
                        alpha = int(DATASET_INFO["Webmap"]["FEMA_FloodHaz"].get("opacity", 0.6) * 255)
                        rgba[i, j] = [r, g, b, alpha]
                    else:
                        rgba[i, j] = [0, 0, 0, 0]
        else:
            # Special handling for stormwater flood raster with discrete values
            if colormap == "flood" and "Stormwater" in input_raster:
                for i in range(height):
                    for j in range(width):
                        if not valid[i, j]:
                            rgba[i, j] = [0, 0, 0, 0]
                        else:
                            value = band[i, j]
                            if value == 1:
                                # Shallow flood: lighter color (start_hex)
                                r, g, b = hex_to_rgb(start_hex)
                                alpha = int(DATASET_INFO["Webmap"]["2080_Stormwater"].get("opacity", 0.6) * 255)
                                rgba[i, j] = [r, g, b, alpha]
                            elif value == 2:
                                # Deep flood: darker color (end_hex)
                                r, g, b = hex_to_rgb(end_hex)
                                alpha = int(DATASET_INFO["Webmap"]["2080_Stormwater"].get("opacity", 0.6) * 255)
                                rgba[i, j] = [r, g, b, alpha]
                            else:
                                rgba[i, j] = [0, 0, 0, 0]
            else:
                # For continuous data (other than stormwater), normalize using min_val and max_val.
                if max_val - min_val == 0:
                    norm = np.zeros_like(band, dtype=float)
                else:
                    norm = np.zeros_like(band, dtype=float)
                    norm[valid] = (band[valid] - min_val) / (max_val - min_val)
                norm = np.nan_to_num(norm, nan=0.0)
                norm = norm.clip(0, 1)
                for i in range(height):
                    for j in range(width):
                        if valid[i, j]:
                            t = norm[i, j]
                            if colormap == "heat" and has_transparency:
                                rgba_color = hex_to_rgba(interpolate_color_with_alpha(t, start_hex, end_hex))
                                rgba[i, j] = rgba_color
                            else:
                                hex_color = interpolate_color(t, start_hex, end_hex)
                                r, g, b = hex_to_rgb(hex_color)
                                if layer_key:
                                    base_opacity = DATASET_INFO["Webmap"][layer_key].get("opacity", 0.7) * 255
                                    if colormap == "heat":
                                        alpha = int(base_opacity * max(0.2, t)) if t > 0.05 else 0
                                    else:
                                        alpha = int(base_opacity * max(0.2, t)) if t > 0 else 0
                                else:
                                    if colormap == "heat":
                                        alpha = int(200 * max(0.2, t)) if t > 0.05 else 0
                                    else:
                                        alpha = int(200 * max(0.2, t)) if t > 0 else 0
                                rgba[i, j] = [r, g, b, alpha]
                        else:
                            rgba[i, j] = [0, 0, 0, 0]
        
        img = Image.fromarray(rgba)
        img.save(output_png)
        print(f"  Successfully saved {output_png}")
        
        if dst_bounds is None or not all(np.isfinite(b) for b in dst_bounds):
            print("WARNING: Invalid bounds detected. Using NYC fallback bounds.")
            dst_bounds = (-74.26, 40.49, -73.69, 40.91)
        
        return output_png, dst_bounds
        
    except Exception as e:
        print(f"Error processing raster {input_raster}: {e}")
        traceback.print_exc()
        print("Creating an empty PNG instead.")
        img = Image.new('RGBA', (500, 500), (0, 0, 0, 0))
        img.save(output_png)
        print("Using NYC fallback bounds.")
        return output_png, (-74.26, 40.49, -73.69, 40.91)

def compute_jenks_breaks(values, n_classes):
    import numpy as np
    # Filter out values that cannot be converted to float
    clean_values = []
    for v in values:
        try:
            val = float(v)
            if np.isfinite(val) and (val < 1e12):
                clean_values.append(val)
        except (ValueError, TypeError):
            continue
    # If no valid values remain, use a default value
    if not clean_values:
        clean_values = [0.0]
    clean_values = np.array(clean_values)
    return [float(b) for b in jenkspy.jenks_breaks(clean_values, n_classes)]

def get_color_from_gradient(value, breaks, color_ramp):
    try:
        val = float(value)
    except (ValueError, TypeError):
        val = 0.0
    start_hex = color_ramp["start"]
    end_hex = color_ramp["end"]
    for i in range(1, len(breaks)):
        if val <= breaks[i]:
            ratio = (val - breaks[i-1]) / (breaks[i] - breaks[i-1]) if breaks[i] != breaks[i-1] else 0
            return interpolate_color(ratio, start_hex, end_hex)
    return end_hex

def get_color_from_multi_gradient(value, breaks, colors):
    try:
        val = float(value)
    except (ValueError, TypeError):
        return colors[0]  # default to first color if conversion fails
    n = len(breaks) - 1
    for i in range(1, n+1):
        if val <= breaks[i]:
            ratio = (val - breaks[i-1]) / (breaks[i] - breaks[i-1]) if breaks[i] != breaks[i-1] else 0
            return interpolate_color(ratio, colors[i-1], colors[i])
    return colors[-1]

###############################################################################
# 3. POPUP HTML & CAPITAL PROJECTS TABLE
###############################################################################
def generate_capital_projects_table(properties):
    PROJECT_FIELDS = ["Title", "CurrentPha", "Construc_4", "ProjectLia"]
    FIELD_ALIASES = {
        "Title": "Project",
        "CurrentPha": "Phase",
        "Construc_4": "Completion",
        "ProjectLia": "Liason"
    }
    data = {}
    for field in PROJECT_FIELDS:
        raw_val = properties.get(field, "")
        if isinstance(raw_val, str):
            data[field] = [v.strip() for v in raw_val.split(",") if v.strip()]
        else:
            data[field] = []
    n = max((len(lst) for lst in data.values()), default=0)
    if n == 0:
        return "<p>No recent capital projects.</p>"
    
    header = "<tr>" + "".join(f"<th>{FIELD_ALIASES.get(f, f)}</th>" for f in PROJECT_FIELDS) + "</tr>"
    rows = [header]
    for i in range(n):
        row = "<tr>"
        for f in PROJECT_FIELDS:
            val = data[f][i] if i < len(data[f]) else ""
            row += f"<td>{val}</td>"
        row += "</tr>"
        rows.append(row)
    return f"<table class='popup-table'>{''.join(rows)}</table>"

def generate_feature_html(properties):
    park_name = properties.get("signname", "Unknown Park")
    title_html = f'<div class="popup-header" style="padding-top: 10px; padding-bottom: 10px;">{park_name}</div>'

    suitability = properties.get("suitability", 0)
    suitability_str = f"{suitability:.2f}"
    high_impact_color = interpolate_color(suitability, "#ff0000", "#00ff00")
    bubble_high_impact = f"""
    <div class="info-bubble" style="text-align:center;">
      <h4>High-Impact Investment Opportunity: <span style="color:{high_impact_color};">{suitability_str}</span></h4>
    </div>
    """

    raw_total = properties.get("EstInvTotal", 0)
    try:
        total_investment = f"{float(raw_total):,.0f}"
    except:
        total_investment = str(raw_total)
    
    inv_norm_opacity = properties.get("Inv_Norm", 0)
    bubble_investments = f"""
    <div class="info-bubble" style="text-align:center;">
      <h4>Estimated Recent Investments:<br>${total_investment} (since {cutoff_date_simple})</h4>
      <div class="icon-row" style="margin-top:10px; justify-content:center;">
        <div class="icon-col">
          <div class="circle-bg">
            <img src="{ICONS_DIR}/{INDEX_ICONS['Capital']}" 
                 class="circle-icon" 
                 style="opacity:{inv_norm_opacity};" />
            <img src="{OUTLINE_SVG}" class="icon-outline" style="opacity:1;" />
          </div>
          <div class="icon-label">Capital</div>
        </div>
      </div>
      <details class="collapsible" style="margin-top:10px;">
        <summary style="display:flex; justify-content: space-between; align-items:center; cursor:pointer;">
          <span>Recent Capital Projects</span>
          <span style="font-weight:bold;">▼</span>
        </summary>
        <div class="scrollable-table" style="padding:8px;">
          {generate_capital_projects_table(properties)}
        </div>
      </details>
    </div>
    """
    
    hazard_factor = properties.get("hazard_factor", 0)
    heat_opacity = properties.get("HeatHaz", 0)
    coastal_opacity = properties.get("CoastalFloodHaz", 0)
    storm_opacity = properties.get("StormFloodHaz", 0)
    bubble_hazard = f"""
    <div class="info-bubble" style="text-align:center;">
      <h4>Overall Hazard Rating: {hazard_factor:.2f}</h4>
      <div class="icon-row" style="margin-top:10px; justify-content:center;">
        <div class="icon-col">
          <div class="circle-bg">
            <img src="{ICONS_DIR}/{INDEX_ICONS['Extreme Heat']}" 
                 class="circle-icon" 
                 style="opacity:{heat_opacity};" />
            <img src="{OUTLINE_SVG}" class="icon-outline" style="opacity:1;" />
          </div>
          <div class="icon-label">Extreme Heat</div>
        </div>
        <div class="icon-col">
          <div class="circle-bg">
            <img src="{ICONS_DIR}/{INDEX_ICONS['Coastal Flooding']}" 
                 class="circle-icon" 
                 style="opacity:{coastal_opacity};" />
            <img src="{OUTLINE_SVG}" class="icon-outline" style="opacity:1;" />
          </div>
          <div class="icon-label">Coastal Flooding</div>
        </div>
        <div class="icon-col">
          <div class="circle-bg">
            <img src="{ICONS_DIR}/{INDEX_ICONS['Stormwater Flooding']}" 
                 class="circle-icon" 
                 style="opacity:{storm_opacity};" />
            <img src="{OUTLINE_SVG}" class="icon-outline" style="opacity:1;" />
          </div>
          <div class="icon-label">Stormwater Flooding</div>
        </div>
      </div>
    </div>
    """
    
    vul_factor = properties.get("vul_factor", 0)
    hv_opacity = properties.get("HeatVuln", 0)
    fv_opacity = properties.get("FloodVuln", 0)
    bubble_vulnerability = f"""
    <div class="info-bubble" style="text-align:center;">
      <h4>Overall Vulnerability Rating: {vul_factor:.2f}</h4>
      <div class="icon-row" style="margin-top:10px; justify-content:center;">
        <div class="icon-col">
          <div class="circle-bg">
            <img src="{ICONS_DIR}/{INDEX_ICONS['Heat Vulnerability']}" 
                 class="circle-icon" 
                 style="opacity:{hv_opacity};" />
            <img src="{OUTLINE_SVG}" class="icon-outline" style="opacity:1;" />
          </div>
          <div class="icon-label">Heat Vulnerability</div>
        </div>
        <div class="icon-col">
          <div class="circle-bg">
            <img src="{ICONS_DIR}/{INDEX_ICONS['Flood Vulnerability']}" 
                 class="circle-icon" 
                 style="opacity:{fv_opacity};" />
            <img src="{OUTLINE_SVG}" class="icon-outline" style="opacity:1;" />
          </div>
          <div class="icon-label">Flood Vulnerability</div>
        </div>
      </div>
    </div>
    """
    
    return f"""
    <div class="popup-content">
      {title_html}
      {bubble_high_impact}
      {bubble_investments}
      {bubble_hazard}
      {bubble_vulnerability}
    </div>
    """

###############################################################################
# 4. STYLE FUNCTION FOR PARKS (BY SUITABILITY)
###############################################################################
def style_function(feature):
    suitability = feature['properties'].get("suitability", 0)
    ramp = DATASET_INFO["Webmap"]["Suitability"]["color_ramp"]
    fill_color = interpolate_color(suitability, ramp["start"], ramp["end"])
    return {
        "fillColor": fill_color,
        "color": fill_color,
        "weight": 2,
        "fillOpacity": 0.6,
    }

###############################################################################
# 5. MAIN WEBMAP GENERATION
###############################################################################
def generate_webmap():
    import shutil
    web_dir = os.path.join(OUTPUT_DIR, "web_layers")
    if os.path.exists(web_dir):
        shutil.rmtree(web_dir)
    os.makedirs(web_dir, exist_ok=True)

    m = folium.Map(location=[40.7128, -74.0060], zoom_start=10, tiles='CartoDB Positron')
    m.get_root().html.add_child(folium.Element(STYLE_BLOCK))
    
    # Create directory for processed web layers
    web_dir = os.path.join(OUTPUT_DIR, "web_layers")
    os.makedirs(web_dir, exist_ok=True)
    
    # Process and cache raster layers
    heat_png = os.path.join(web_dir, "heat.png")
    heat_bounds = None
    if not os.path.exists(heat_png):
        if os.path.exists(HEAT_FILE):
            print(f"Processing heat raster from {HEAT_FILE}")
            heat_png, heat_bounds = process_raster_for_web(HEAT_FILE, heat_png, target_crs="EPSG:4326", colormap="heat")
        else:
            print(f"WARNING: Heat raster file does not exist at {HEAT_FILE}")

    fema_png = os.path.join(web_dir, "fema.png")
    fema_bounds = None
    if not os.path.exists(fema_png):
        if os.path.exists(FEMA_RASTER):
            print(f"Processing FEMA raster from {FEMA_RASTER}")
            fema_png, fema_bounds = process_raster_for_web(FEMA_RASTER, fema_png, target_crs="EPSG:4326", colormap="flood")
        else:
            print(f"WARNING: FEMA raster file does not exist at {FEMA_RASTER}")

    storm_png = os.path.join(web_dir, "storm.png")
    storm_bounds = None
    if not os.path.exists(storm_png):
        if os.path.exists(STORM_RASTER):
            print(f"Processing storm raster from {STORM_RASTER}")
            storm_png, storm_bounds = process_raster_for_web(STORM_RASTER, storm_png, target_crs="EPSG:4326", colormap="flood")
        else:
            print(f"WARNING: Storm raster file does not exist at {STORM_RASTER}")

    # --- Vulnerability Layers Integration ---

    # Add Heat Vulnerability (HVI) layer
    if os.path.exists(HVI_DATA):
        hvi_gdf = gpd.read_file(HVI_DATA).to_crs(epsg=4326)
        def style_hvi(feature):
            try:
                val = float(feature['properties'].get("HVI", 1))
            except:
                val = 1
            # Map values 1-5 to a color using the provided 5-class color ramp.
            colors = DATASET_INFO["HVI"]["color_ramp"]["colors"]
            idx = min(max(int(round(val)) - 1, 0), len(colors) - 1)
            return {"fillColor": colors[idx],
                    "color": colors[idx],
                    "weight": 1,
                    "fillOpacity": DATASET_INFO["HVI"].get("opacity", 0.3)}
        folium.GeoJson(
             hvi_gdf,
             name=DATASET_INFO["HVI"]["name"],
             style_function=style_hvi,
             overlay=True,
             control=True,
             show=False
        ).add_to(m)
    else:
        print("WARNING: HVI data not found.")

    # Add Flood Vulnerability layers from FVI_DATA
    if os.path.exists(FVI_DATA):
        fvi_gdf = gpd.read_file(FVI_DATA).to_crs(epsg=4326)
        # Flood Vulnerability – SS layer
        def style_fvi_ss(feature):
            try:
                val = float(feature['properties'].get("ss_80s", 1))
            except:
                val = 1
            # For a discrete mapping, use the "start" color for lower values and "end" for higher values.
            color = DATASET_INFO["Flood_Vulnerability_SS"]["color_ramp"]["start"] if val <= 3 else DATASET_INFO["Flood_Vulnerability_SS"]["color_ramp"]["end"]
            return {"fillColor": color,
                    "color": color,
                    "weight": 1,
                    "fillOpacity": DATASET_INFO["Flood_Vulnerability_SS"].get("opacity", 0.15)}
        folium.GeoJson(
             fvi_gdf,
             name=DATASET_INFO["Flood_Vulnerability_SS"]["name"],
             style_function=style_fvi_ss,
             overlay=True,
             control=True,
             show=False
        ).add_to(m)
        # Flood Vulnerability – TID layer
        def style_fvi_tid(feature):
            try:
                val = float(feature['properties'].get("tid_80s", 1))
            except:
                val = 1
            color = DATASET_INFO["Flood_Vulnerability_TID"]["color_ramp"]["start"] if val <= 3 else DATASET_INFO["Flood_Vulnerability_TID"]["color_ramp"]["end"]
            return {"fillColor": color,
                    "color": color,
                    "weight": 1,
                    "fillOpacity": DATASET_INFO["Flood_Vulnerability_TID"].get("opacity", 0.15)}
        folium.GeoJson(
             fvi_gdf,
             name=DATASET_INFO["Flood_Vulnerability_TID"]["name"],
             style_function=style_fvi_tid,
             overlay=True,
             control=True,
             show=False
        ).add_to(m)
    else:
        print("WARNING: FVI data not found.")

    # Load Parks GeoJSON but don't add it yet
    gdf = gpd.read_file(OUTPUT_GEOJSON)
    gdf = gdf.to_crs(epsg=4326)
    gdf["popup_html"] = gdf.apply(lambda row: generate_feature_html(row.to_dict()), axis=1)
    geojson_data = gdf.to_json()

    # Get bounds for overlay alignment
    bounds = gdf.total_bounds  # [minx, miny, maxx, maxy]
    nyc_bounds = [[bounds[1], bounds[0]], [bounds[3], bounds[2]]]

    # Add raster overlays
    heat_config = DATASET_INFO["Webmap"]["Summer_Temperature"]
    if heat_bounds:
        heat_overlay_bounds = [[heat_bounds[1], heat_bounds[0]], [heat_bounds[3], heat_bounds[2]]]
        print(f"Adding heat overlay with bounds: {heat_overlay_bounds}")
        folium.raster_layers.ImageOverlay(
            image=heat_png,
            bounds=heat_overlay_bounds,
            name=heat_config["name"],
            opacity=heat_config.get("opacity", 0.7), 
            show=True,
            alt=heat_config["name"]
        ).add_to(m)
    else:
        print("WARNING: No valid bounds for heat overlay. Layer will not be added.")

    fema_config = DATASET_INFO["Webmap"]["FEMA_FloodHaz"]
    if fema_bounds:
        fema_overlay_bounds = [[fema_bounds[1], fema_bounds[0]], [fema_bounds[3], fema_bounds[2]]]
        print(f"Adding FEMA overlay with bounds: {fema_overlay_bounds}")
        folium.raster_layers.ImageOverlay(
            image=fema_png,
            bounds=fema_overlay_bounds,
            name=fema_config["name"],
            opacity=fema_config.get("opacity", 0.7), 
            show=True,
            alt=fema_config["name"]
        ).add_to(m)
    else:
        print("WARNING: No valid bounds for FEMA overlay. Layer will not be added.")

    storm_config = DATASET_INFO["Webmap"]["2080_Stormwater"]
    if storm_bounds:
        storm_overlay_bounds = [[storm_bounds[1], storm_bounds[0]], [storm_bounds[3], storm_bounds[2]]]
        print(f"Adding storm overlay with bounds: {storm_overlay_bounds}")
        folium.raster_layers.ImageOverlay(
            image=storm_png,
            bounds=storm_overlay_bounds,
            name=storm_config["name"],
            opacity=storm_config.get("opacity", 0.7), 
            show=True,
            alt=storm_config["name"]
        ).add_to(m)
    else:
        print("WARNING: No valid bounds for storm overlay. Layer will not be added.")
    
    # Now add the Parks layer LAST so it's on top
    popup = folium.GeoJsonPopup(
        fields=["popup_html"],
        aliases=[None],
        labels=False,
        parse_html=True
    )
    
    # Add parks layer with a higher z-index to ensure it's on top
    folium.GeoJson(
        data=geojson_data,
        name="NYC Parks",
        style_function=style_function,
        popup=popup,
        overlay=True,
        control=True,
        show=True,
        highlight_function=lambda x: {'weight': 3, 'fillOpacity': 0.8}  # Enhance highlight effect
    ).add_to(m)
    
    # Add layer control
    folium.LayerControl().add_to(m)
    
    # Add custom JavaScript to ensure parks layer is always on top
    js = """
    <script>
    document.addEventListener('DOMContentLoaded', function() {
        // Get the map
        var map = document.querySelector('.folium-map').map;
        
        // Function to bring parks layer to front
        function bringParksToFront() {
            map.eachLayer(function(layer) {
                // Check if this is the parks layer
                if (layer.options && layer.options.name === "NYC Parks") {
                    layer.bringToFront();
                }
            });
        }
        
        // Run initially after map loads
        setTimeout(bringParksToFront, 1000);
        
        // Run whenever a layer is added or removed
        map.on('layeradd', bringParksToFront);
        map.on('layerremove', bringParksToFront);
        
        // Run when zoom ends (in case overlapping changes)
        map.on('zoomend', bringParksToFront);
    });
    </script>
    """
    m.get_root().html.add_child(folium.Element(js))
    
    # Optional: dynamic layer visibility based on zoom level
    min_zoom_level = 13
    script = f"""
    <script>
      var map = document.querySelector('.folium-map').map;
      function updateVisibility() {{
          var currentZoom = map.getZoom();
          var showLayers = currentZoom >= {min_zoom_level};
          document.querySelectorAll('img.leaflet-image-layer').forEach(function(img){{
              img.style.opacity = showLayers ? img.getAttribute('data-original-opacity') : "0";
          }});
      }}
      map.on('zoomend', updateVisibility);
      updateVisibility();
    </script>
    """
    m.get_root().html.add_child(folium.Element(script))
    
    # Unified legend: includes Summer Temperature and Flood Vulnerability gradients
    legend_html = f"""
    <div id="unified-legend" style="{LEGEND_STYLES['container']}">
        <h4 style="{LEGEND_STYLES['header']}">Legend</h4>
        
        <!-- Parks Suitability section -->
        <div id="parks-section">
            <h5 style="{LEGEND_STYLES['sectionHeader']}">Parks Suitability</h5>
            <div style="width: 200px; height: 20px; background: linear-gradient(to right, {DATASET_INFO["Webmap"]["Suitability"]["color_ramp"]["start"]}, {DATASET_INFO["Webmap"]["Suitability"]["color_ramp"]["end"]});">
            </div>
            <div style="display: flex; justify-content: space-between; width: 200px; margin-bottom: 10px;">
                <span style="{LEGEND_STYLES['label']}">Lower Priority</span>
                <span style="{LEGEND_STYLES['label']}">Higher Priority</span>
            </div>
        </div>
        
        <!-- Heat Hazard section -->
        <div id="heat-hazard-section">
            <h5 style="{LEGEND_STYLES['sectionHeader']}">Heat Hazard</h5>
            <div style="width: 200px; height: 20px; background: linear-gradient(to right, {DATASET_INFO["Webmap"]["Summer_Temperature"]["color_ramp"]["start"]}, {DATASET_INFO["Webmap"]["Summer_Temperature"]["color_ramp"]["end"]}); opacity: {DATASET_INFO["Webmap"]["Summer_Temperature"].get("opacity", 0.7)};">
            </div>
            <div style="display: flex; justify-content: space-between; width: 200px; margin-bottom: 10px;">
                <span style="{LEGEND_STYLES['label']}">Lower</span>
                <span style="{LEGEND_STYLES['label']}">Higher</span>
            </div>
        </div>
        
        <!-- FEMA Floodmap section -->
        <div id="fema-section" style="display: none;">
            <h5 style="{LEGEND_STYLES['sectionHeader']}">FEMA Floodmap</h5>
            <div style="{LEGEND_STYLES['itemContainer']}">
                <div style="{LEGEND_STYLES['colorBox']} background-color: {DATASET_INFO["Webmap"]["FEMA_FloodHaz"]["hex_1pct"]}; opacity: {DATASET_INFO["Webmap"]["FEMA_FloodHaz"].get("opacity", 0.6)};"></div>
                <span style="{LEGEND_STYLES['label']}">100-year (1%) Flood Zone</span>
            </div>
            <div style="{LEGEND_STYLES['itemContainer']}">
                <div style="{LEGEND_STYLES['colorBox']} background-color: {DATASET_INFO["Webmap"]["FEMA_FloodHaz"]["hex_0_2pct"]}; opacity: {DATASET_INFO["Webmap"]["FEMA_FloodHaz"].get("opacity", 0.6)};"></div>
                <span style="{LEGEND_STYLES['label']}">500-year (0.2%) Flood Zone</span>
            </div>
        </div>
        
        <!-- Stormwater Flood section -->
        <div id="storm-section">
            <h5 style="{LEGEND_STYLES['sectionHeader']}">2080 Stormwater Flooding</h5>
            <div style="{LEGEND_STYLES['itemContainer']}">
                <div style="{LEGEND_STYLES['colorBox']} background-color: {DATASET_INFO["Webmap"]["2080_Stormwater"]["color_ramp"]["start"]}; opacity: {DATASET_INFO["Webmap"]["2080_Stormwater"].get("opacity", 0.6)};"></div>
                <span style="{LEGEND_STYLES['label']}">Shallow Stormwater Flooding</span>
            </div>
            <div style="{LEGEND_STYLES['itemContainer']}">
                <div style="{LEGEND_STYLES['colorBox']} background-color: {DATASET_INFO["Webmap"]["2080_Stormwater"]["color_ramp"]["end"]}; opacity: {DATASET_INFO["Webmap"]["2080_Stormwater"].get("opacity", 0.6)};"></div>
                <span style="{LEGEND_STYLES['label']}">Deep Stormwater Flooding</span>
            </div>
        </div>
        
        <!-- Vulnerability Layers section (default off) -->
        <div id="vulnerability-section" style="display: none;">
            <h5 style="{LEGEND_STYLES['sectionHeader']}">Vulnerability Layers</h5>
            <!-- Heat Vulnerability -->
            <div id="heat-vulnerability-legend">
                <h6 style="{LEGEND_STYLES['sectionHeader']} font-size: 13px;">Heat Vulnerability Index</h6>
                <div style="width: 200px; height: 20px; background: linear-gradient(to right, {DATASET_INFO["HVI"]["color_ramp"]["colors"][0]}, {DATASET_INFO["HVI"]["color_ramp"]["colors"][-1]}); opacity: {DATASET_INFO["HVI"].get("opacity", 0.3)};">
                </div>
            </div>
            <!-- Flood Vulnerability: SS and TID -->
            <div id="flood-vulnerability-legend">
                <h6 style="{LEGEND_STYLES['sectionHeader']} font-size: 13px;">Flood Vulnerability</h6>
                <div style="display: flex; gap: 10px;">
                    <div style="width: 90px; height: 20px; background: linear-gradient(to right, {DATASET_INFO["Flood_Vulnerability_SS"]["color_ramp"]["start"]}, {DATASET_INFO["Flood_Vulnerability_SS"]["color_ramp"]["end"]}); opacity: {DATASET_INFO["Flood_Vulnerability_SS"].get("opacity", 0.15)};"></div>
                    <div style="width: 90px; height: 20px; background: linear-gradient(to right, {DATASET_INFO["Flood_Vulnerability_TID"]["color_ramp"]["start"]}, {DATASET_INFO["Flood_Vulnerability_TID"]["color_ramp"]["end"]}); opacity: {DATASET_INFO["Flood_Vulnerability_TID"].get("opacity", 0.15)};"></div>
                </div>
                <div style="display: flex; justify-content: space-between; width: 200px; margin-bottom: 10px;">
                    <span style="{LEGEND_STYLES['label']}">SS (Low to High)</span>
                    <span style="{LEGEND_STYLES['label']}">TID (Low to High)</span>
                </div>
            </div>
        </div>
    </div>
    """

    legend_script = """
    <script>
    document.addEventListener('DOMContentLoaded', function() {
        var map = document.querySelector('.folium-map').map;
        function updateLegend() {
            var heatOn = false;
            var femaOn = false;
            var stormOn = false;
            var vulnerabilityOn = false;
            
            var layerInputs = document.querySelectorAll('.leaflet-control-layers-overlays input');
            layerInputs.forEach(function(input) {
                if (input.checked) {
                    var labelText = input.nextElementSibling.textContent.trim();
                    if (labelText.includes("Summer Temperature") || labelText.includes("Heat Hazard")) {
                        heatOn = true;
                    }
                    if (labelText.includes("FEMA Floodmap")) {
                        femaOn = true;
                    }
                    if (labelText.includes("2080 Stormwater Flooding")) {
                        stormOn = true;
                    }
                    if (labelText.includes("Heat Vulnerability") || labelText.includes("Flood Vulnerability")) {
                        vulnerabilityOn = true;
                    }
                }
            });
            document.getElementById('heat-hazard-section').style.display = heatOn ? 'block' : 'none';
            document.getElementById('fema-section').style.display = femaOn ? 'block' : 'none';
            document.getElementById('storm-section').style.display = stormOn ? 'block' : 'none';
            document.getElementById('vulnerability-section').style.display = vulnerabilityOn ? 'block' : 'none';
        }
        setTimeout(updateLegend, 1000);
        map.on('layeradd', updateLegend);
        map.on('layerremove', updateLegend);
        map.on('zoomend', updateLegend);
    });
    </script>
    """

    m.get_root().html.add_child(folium.Element(legend_html + legend_script))
    
    m.save(OUTPUT_WEBMAP)
    print("Webmap generated and saved to:", OUTPUT_WEBMAP)

if __name__ == "__main__":
    generate_webmap()