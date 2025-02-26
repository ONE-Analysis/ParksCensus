import folium
import geopandas as gpd
import json
import os
import re
from datetime import datetime
import rasterio
from rasterio.enums import Resampling
from pathlib import Path

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
    STORM_RASTER
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
    /* Space the columns evenly across the row */
    justify-content: space-evenly; 
    align-items: center; 
    gap: 10px;
  }
  .icon-col {
    /* Each column will stack icon + label vertically, centered */
    display: flex;
    flex-direction: column;
    align-items: center;
  }

  /* Container for the icon and outline, both 60x60 */
  .circle-bg {
    position: relative;
    width: 60px;
    height: 60px;
    background: none;
  }
  .circle-icon {
    width: 60px;
    height: 60px;
    /* We'll set opacity inline for each icon */
  }
  .icon-outline {
    width: 60px;
    height: 60px;
    position: absolute;
    top: 0;
    left: 0;
    opacity: 1; /* always full opacity */
  }

  .icon-label {
    font-size: 12px;
    margin-top: 4px;
    color: #333;
  }
</style>
"""

###############################################################################
# 2. HELPER FUNCTIONS
###############################################################################
def hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

def rgb_to_hex(rgb):
    return '#{:02x}{:02x}{:02x}'.format(*rgb)

def interpolate_color(val, start_hex, end_hex):
    """Interpolate between start_hex and end_hex based on val in [0..1]."""
    start_rgb = hex_to_rgb(start_hex)
    end_rgb = hex_to_rgb(end_hex)
    interp = tuple(int(s + (e - s) * val) for s, e in zip(start_rgb, end_rgb))
    return rgb_to_hex(interp)

def format_value(val):
    """Round numeric to two decimals if possible, else return original."""
    try:
        num = float(val)
        return f"{num:.2f}"
    except (ValueError, TypeError):
        return val

def create_downsampled_raster(input_raster, output_raster, scale_factor=0.1, target_crs="EPSG:4326"):
    import rasterio
    from rasterio.warp import calculate_default_transform, reproject, Resampling
    from affine import Affine

    # First, reproject the input raster to the target CRS
    with rasterio.open(input_raster) as src:
        transform, width, height = calculate_default_transform(
            src.crs, target_crs, src.width, src.height, *src.bounds)
        kwargs = src.meta.copy()
        kwargs.update({
            'crs': target_crs,
            'transform': transform,
            'width': width,
            'height': height
        })
        # Save the reprojected raster temporarily
        reprojected_raster = output_raster.replace(".tif", "_reproj.tif")
        with rasterio.open(reprojected_raster, 'w', **kwargs) as dst:
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

    # Now downsample the reprojected raster
    with rasterio.open(reprojected_raster) as src2:
        new_width = int(src2.width * scale_factor)
        new_height = int(src2.height * scale_factor)
        new_transform = src2.transform * Affine.scale(src2.width / new_width, src2.height / new_height)
        kwargs2 = src2.meta.copy()
        kwargs2.update({
            'width': new_width,
            'height': new_height,
            'transform': new_transform
        })
        data = src2.read(
            out_shape=(src2.count, new_height, new_width),
            resampling=Resampling.average
        )
        with rasterio.open(output_raster, 'w', **kwargs2) as dst2:
            dst2.write(data)
    return output_raster

def raster_to_png(input_raster, output_png, colormap=None, raster_type=None):
    import numpy as np
    from PIL import Image
    import rasterio

    # Open the raster and read the first band.
    with rasterio.open(input_raster) as src:
        data = src.read(1)
        nodata = src.nodata

    # Create a mask for valid data.
    mask = (data != nodata) if nodata is not None else np.ones(data.shape, dtype=bool)
    height, width = data.shape
    rgba = np.zeros((height, width, 4), dtype=np.uint8)

    # Helper functions for color interpolation.
    def hex_to_rgb(hex_color):
        hex_color = hex_color.lstrip('#')
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    
    def rgb_to_hex(rgb):
        return '#{:02x}{:02x}{:02x}'.format(*rgb)
    
    def interpolate_color(val, start_hex, end_hex):
        start_rgb = hex_to_rgb(start_hex)
        end_rgb = hex_to_rgb(end_hex)
        interp_rgb = tuple(int(s + (e - s) * val) for s, e in zip(start_rgb, end_rgb))
        return rgb_to_hex(interp_rgb)

    if colormap == "heat":
        # Get the color ramp from your config.
        ramp = DATASET_INFO["Webmap"]["Summer_Temperature"]["color_ramp"]
        start_hex = ramp.get("start", "#0000ff")  # Default to blue
        end_hex = ramp.get("end", "#ff0000")       # Default to red
        
        # Trim any extra alpha information.
        if len(start_hex) > 7:
            start_hex = start_hex[:7]
        if len(end_hex) > 7:
            end_hex = end_hex[:7]

        valid_data = data[mask]
        if valid_data.size > 0:
            # Normalize using the 1st and 99th percentiles.
            min_val = np.percentile(valid_data, 1)
            max_val = np.percentile(valid_data, 99)
            # Avoid division by zero.
            if max_val - min_val == 0:
                norm = np.zeros_like(data, dtype=float)
            else:
                norm = (data - min_val) / (max_val - min_val)
            norm = np.clip(norm, 0, 1)

            # Apply the color ramp to each valid pixel.
            for i in range(height):
                for j in range(width):
                    if mask[i, j]:
                        t = norm[i, j]
                        hex_color = interpolate_color(t, start_hex, end_hex)
                        r, g, b = hex_to_rgb(hex_color)
                        rgba[i, j] = [r, g, b, 200]  # Semi-transparent
                    else:
                        rgba[i, j] = [0, 0, 0, 0]
        else:
            rgba[:, :, :] = 0
    else:
        # Fallback: render as grayscale.
        valid_data = data[mask]
        max_val = np.max(valid_data) if valid_data.size > 0 else 1
        for i in range(height):
            for j in range(width):
                if mask[i, j]:
                    val = int(255 * (data[i, j] / max_val))
                    rgba[i, j] = [val, val, val, 255]
                else:
                    rgba[i, j] = [0, 0, 0, 0]

    # Save the output PNG.
    img = Image.fromarray(rgba)
    img.save(output_png)
    return output_png

###############################################################################
# 3. CAPITAL PROJECTS TABLE
###############################################################################
def generate_capital_projects_table(properties):
    """Parses comma-separated fields for capital projects into a scrollable table."""
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
    
    # Build the table
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

###############################################################################
# 4. POPUP HTML: MIMICS YOUR MOCKUP
###############################################################################
def generate_feature_html(properties):
    """Builds the popup HTML with:
       - a title (park name)
       - Bubble 1: Investments (with Capital icon)
       - Bubble 2: Hazard rating (3 icons)
       - Bubble 3: Vulnerability rating (2 icons)
       Where each icon's opacity is set from an index,
       but the outline remains fully opaque.
    """
    # 0) Title (Park Name)
    # Existing title (park name)
    park_name = properties.get("signname", "Unknown Park")
    title_html = f'<div class="popup-header">{park_name}</div>'


    # 1) High-Impact Investment OpportunityBubble
    suitability = properties.get("suitability", 0)
    suitability_str = f"{suitability:.2f}"
    # Compute a red-to-green color (red for low, green for high)
    high_impact_color = interpolate_color(suitability, "#ff0000", "#00ff00")
    bubble_high_impact = f"""
    <div class="info-bubble" style="text-align:center;">
      <h4>High-Impact Investment Opportunity: <span style="color:{high_impact_color};">{suitability_str}</span></h4>
    </div>
    """

    # 2) Investments Bubble
    raw_total = properties.get("EstInvTotal", 0)
    try:
        total_investment = f"{float(raw_total):,.0f}"
    except:
        total_investment = str(raw_total)
    
    # 'Capital' icon opacity from Inv_Norm
    inv_norm_opacity = properties.get("Inv_Norm", 0)  # 0..1
    
    bubble_investments = f"""
    <div class="info-bubble" style="text-align:center;">
      <h4>Estimated Recent Investments:<br>${total_investment} (since {cutoff_date_simple})</h4>
      <div class="icon-row" style="margin-top:10px; justify-content:center;">
        <div class="icon-col">
          <div class="circle-bg">
            <!-- Icon has the alpha -->
            <img src="{ICONS_DIR}/{INDEX_ICONS['Capital']}" 
                 class="circle-icon" 
                 style="opacity:{inv_norm_opacity};" />
            <!-- Outline is always full opacity -->
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
    
    # 3) Hazard Bubble
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
    
    # 4) Vulnerability Bubble
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
    
    # 5) Combine everything
    park_title = f'<div class="popup-header">{park_name}</div>'

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
# 5. STYLE FUNCTION FOR PARKS (COLOR BY SUITABILITY)
###############################################################################
def style_function(feature):
    # Use the normalized 'suitability' value to interpolate the fill color
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
# 6. MAIN WEBMAP GENERATION
###############################################################################
def generate_webmap():
    # Create Folium map
    m = folium.Map(location=[40.7128, -74.0060], zoom_start=10, tiles='CartoDB Positron')
    
    # Inject our CSS styles
    m.get_root().html.add_child(folium.Element(STYLE_BLOCK))
    
    # Create directory for downsampled rasters
    web_dir = os.path.join(OUTPUT_DIR, "web_layers")
    os.makedirs(web_dir, exist_ok=True)
    
    # Downsample & convert the heat raster
    heat_small = os.path.join(web_dir, "heat_small.tif")
    heat_png = os.path.join(web_dir, "heat.png")
    if not os.path.exists(heat_png):
        create_downsampled_raster(HEAT_FILE, heat_small, 0.1)
        raster_to_png(heat_small, heat_png, colormap="heat")
    
    # FEMA flood
    fema_small = os.path.join(web_dir, "fema_small.tif")
    fema_png = os.path.join(web_dir, "fema.png")
    if not os.path.exists(fema_png):
        create_downsampled_raster(FEMA_RASTER, fema_small, 0.1)
        raster_to_png(fema_small, fema_png, colormap="flood", raster_type="FEMA")
    
    # Stormwater flood
    storm_small = os.path.join(web_dir, "storm_small.tif")
    storm_png = os.path.join(web_dir, "storm.png")
    if not os.path.exists(storm_png):
        create_downsampled_raster(STORM_RASTER, storm_small, 0.1)
        raster_to_png(storm_small, storm_png, colormap="flood", raster_type="Stormwater")
    
    # Load Parks GeoJSON
    gdf = gpd.read_file(OUTPUT_GEOJSON)
    gdf = gdf.to_crs(epsg=4326)
    # Build popup HTML for each feature
    gdf["popup_html"] = gdf.apply(lambda row: generate_feature_html(row.to_dict()), axis=1)
    
    # Add to folium
    geojson_data = gdf.to_json()
    popup = folium.GeoJsonPopup(
        fields=["popup_html"],
        aliases=[None],  # or [""]
        labels=False,
        parse_html=True
    )    

    folium.GeoJson(
        data=geojson_data,
        name="NYC Parks",
        style_function=style_function,
        popup=popup
    ).add_to(m)
    
    # Add raster overlays
    nyc_bounds = [[gdf.total_bounds[1], gdf.total_bounds[0]],
                  [gdf.total_bounds[3], gdf.total_bounds[2]]]

    heat_config = DATASET_INFO["Webmap"]["Summer_Temperature"]
    folium.raster_layers.ImageOverlay(
        image=heat_png,
        bounds=nyc_bounds,
        name=heat_config["name"],
        opacity=0.7,
        show=False,
        alt=heat_config["name"]
    ).add_to(m)

    fema_config = DATASET_INFO["Webmap"]["FEMA_FloodHaz"]
    folium.raster_layers.ImageOverlay(
        image=fema_png,
        bounds=nyc_bounds,
        name=fema_config["name"],
        opacity=0.6,
        show=False,
        alt=fema_config["name"]
    ).add_to(m)

    storm_config = DATASET_INFO["Webmap"]["2080_Stormwater"]
    folium.raster_layers.ImageOverlay(
        image=storm_png,
        bounds=nyc_bounds,
        name=storm_config["name"],
        opacity=0.6,
        show=False,
        alt=storm_config["name"]
    ).add_to(m)

    folium.LayerControl().add_to(m)
    
    # Optional: dynamic layer visibility based on zoom
    min_zoom_level = 13
    script = f"""
    <script>
      var map = document.querySelector('.folium-map').map;
      var heatLayer = document.querySelector('img[alt="{heat_config["name"]}"]');
      var femaLayer = document.querySelector('img[alt="{fema_config["name"]}"]');
      var stormLayer = document.querySelector('img[alt="{storm_config["name"]}"]');
      
      function updateVisibility() {{
          var currentZoom = map.getZoom();
          var showLayers = currentZoom >= {min_zoom_level};
          
          if (heatLayer && map.hasLayer(heatLayer._layer)) {{
              heatLayer.style.opacity = showLayers ? "0.7" : "0";
          }}
          if (femaLayer && map.hasLayer(femaLayer._layer)) {{
              femaLayer.style.opacity = showLayers ? "0.6" : "0";
          }}
          if (stormLayer && map.hasLayer(stormLayer._layer)) {{
              stormLayer.style.opacity = showLayers ? "0.6" : "0";
          }}
      }}
      map.on('zoomend', updateVisibility);
      updateVisibility();
    </script>
    """
    m.get_root().html.add_child(folium.Element(script))

###############################################################################
# 7. LEGEND
###############################################################################

    # Define the HTML for your unified legend
    legend_html = """
    <div id="unified-legend" style="position: fixed; bottom: 50px; right: 50px; z-index:9999; 
        background: white; padding: 10px; border: 1px solid grey; border-radius: 5px;">
        <h4 style="margin-top:0; margin-bottom: 10px;">Map Legend</h4>
        
        <!-- Temperature section -->
        <div id="temp-section" style="display: none;">
            <h5 style="margin-top:0; margin-bottom: 5px;">Summer Temperature</h5>
            <div style="display: flex; align-items: center; margin-bottom: 5px;">
                <div style="width: 200px; height: 20px; 
                          background: linear-gradient(to right, #0088ff, #00ffff, #ffff00, #ff0000);">
                </div>
            </div>
            <div style="display: flex; justify-content: space-between; width: 200px; margin-bottom: 10px;">
                <span>Cooler</span>
                <span>Warmer</span>
            </div>
        </div>

        <!-- Flood section -->
        <div id="flood-section" style="display: none;">
            <h5 style="margin-top:0; margin-bottom: 5px;">Flood Hazard</h5>
            <div style="display: flex; align-items: center; margin-bottom: 5px;">
                <div style="width: 200px; height: 20px; 
                          background: linear-gradient(to right, rgba(0,150,255,0.3), rgba(0,150,255,1));">
                </div>
            </div>
            <div style="display: flex; justify-content: space-between; width: 200px;">
                <span>Low</span>
                <span>High</span>
            </div>
        </div>
    </div>
    """

    # Define the JavaScript that shows/hides the appropriate legend sections
    legend_script = """
    <script>
        var map = document.querySelector('.folium-map').map;
        // Replace these alt text strings with the actual 'name' parameter you gave each layer
        var heatLayer = document.querySelector('img[alt="Summer Temperature"]');
        var femaLayer = document.querySelector('img[alt="FEMA Floodmap"]');
        var stormLayer = document.querySelector('img[alt="2080 Stormwater Flooding"]');
        
        var unifiedLegend = document.getElementById('unified-legend');
        var tempSection = document.getElementById('temp-section');
        var floodSection = document.getElementById('flood-section');
        
        function updateLegend() {
            var showTemp = false;
            var showFlood = false;
            
            // Check if heat layer is visible
            if (heatLayer && map.hasLayer(heatLayer._layer) && 
                window.getComputedStyle(heatLayer).opacity > 0 && 
                window.getComputedStyle(heatLayer).display !== 'none') {
                showTemp = true;
            }
            
            // Check if flood layers are visible
            if ((femaLayer && map.hasLayer(femaLayer._layer) && 
                 window.getComputedStyle(femaLayer).opacity > 0 && 
                 window.getComputedStyle(femaLayer).display !== 'none') ||
                (stormLayer && map.hasLayer(stormLayer._layer) && 
                 window.getComputedStyle(stormLayer).opacity > 0 && 
                 window.getComputedStyle(stormLayer).display !== 'none')) {
                showFlood = true;
            }
            
            // Show/hide the appropriate sections
            tempSection.style.display = showTemp ? 'block' : 'none';
            floodSection.style.display = showFlood ? 'block' : 'none';
            
            // Show/hide the entire legend if no sections are visible
            unifiedLegend.style.display = (showTemp || showFlood) ? 'block' : 'none';
        }
        
        // Update the legend whenever layers are toggled or map zoom changes
        map.on('overlayadd', updateLegend);
        map.on('overlayremove', updateLegend);
        map.on('zoomend', updateLegend);
        
        // Initial check (slight delay to ensure layers have rendered)
        setTimeout(updateLegend, 1000);
    </script>
    """

    # Add the legend HTML + script to the map
    m.get_root().html.add_child(folium.Element(legend_html + legend_script))

    # Save map
    m.save(OUTPUT_WEBMAP)
    print("Webmap generated and saved to:", OUTPUT_WEBMAP)

if __name__ == "__main__":
    generate_webmap()