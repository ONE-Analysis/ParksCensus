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
    HAZARD_FACTORS,
    VULNERABILITY_FACTORS,
    HAZARD_SUBINDICES,
    OUTPUT_DIR,
    OUTPUT_GEOJSON,
    OUTPUT_WEBMAP
)

# Column -> Alias mapping for the Capital Projects table:
FIELD_ALIASES = {
    "Title": "Project",
    "CurrentPha": "Phase",
    "Construc_4": "Completion",
    "ProjectLia": "Liason"
}

# List of Capital Project fields to display in columns (in the order you prefer):
PROJECT_FIELDS = [
    "Title",
    "CurrentPha",
    "Construc_4",
    "ProjectLia",
]

# List of hazard/vulnerability fields to display (row by row):
HAZARD_FIELDS = ["HeatHaz", "CoastalFloodHaz", "StormFloodHaz", "HeatVuln", "FloodVuln"]

def parse_construc_4(dt_string):
    """
    Parses a datetime string like '11/01/2023 12:00:00 AM' and returns '11/01/2023'.
    If parsing fails, returns the original string.
    """
    dt_string = dt_string.strip()
    if not dt_string:
        return dt_string
    try:
        dt = datetime.strptime(dt_string, "%m/%d/%Y %I:%M:%S %p")
        return dt.strftime("%m/%d/%Y")
    except:
        return dt_string

def parse_splitted(properties, field_name):
    """
    Safely split the comma-separated string from 'properties[field_name]'.
    Returns a list. If the field doesn't exist or is empty, returns an empty list.
    
    Special handling for 'Construc_4': we parse the date string and remove the time portion.
    """
    if field_name in properties and properties[field_name]:
        splitted = properties[field_name].split(", ")
        # If this is Construc_4, convert each item to a simple date string
        if field_name == "Construc_4":
            splitted = [parse_construc_4(x) for x in splitted]
        return splitted
    return []

def remove_signname_from_title(title, signname):
    """
    Removes the park name (signname) from the start of the Title (including any leading spaces).
    Example: if signname == "Madison Square Park", and Title == "Madison Square Park Title for Project",
    this function will remove 'Madison Square Park ' from the start.
    """
    if not title or not signname:
        return title
    # Build a regex pattern that matches signname + optional spaces at the start
    pattern = r"^" + re.escape(signname) + r"\s*"
    return re.sub(pattern, "", title)

def format_est_inv_total(value):
    """
    Tries to parse the value as float and format with commas (rounded to nearest dollar).
    If parsing fails, returns the original value as-is.
    """
    try:
        val_float = float(value)
        # Format with commas, no decimal
        return f"{val_float:,.0f}"
    except:
        return value

def create_downsampled_raster(input_raster, output_raster, scale_factor=0.1):
    """
    Create a smaller, downsampled version of a raster using rasterio
    """
    import rasterio
    from rasterio.enums import Resampling
    
    with rasterio.open(input_raster) as src:
        # Calculate new dimensions
        width = int(src.width * scale_factor)
        height = int(src.height * scale_factor)
        
        # Define output profile
        kwargs = src.profile.copy()
        kwargs.update({
            'width': width,
            'height': height
        })
        
        # Read and resample data
        data = src.read(
            out_shape=(src.count, height, width),
            resampling=Resampling.average
        )
        
        # Write resampled data
        with rasterio.open(output_raster, 'w', **kwargs) as dst:
            dst.write(data)
            
    return output_raster

def generate_feature_html(properties):
    """
    Popup layout:
      1) Park name at top
      2) 'Estimated Recent Investments' bubble
         - inside it, a hover-to-open dropdown for 'Recent Capital Projects'
      3) 'Climate Risk Factor' bubble
         - inside it, a single hover-to-open dropdown for 'Hazard'
           (Vulnerability is omitted, since it has no sub-indices and we don't want it in the popup)
         - plus the 5 icons (3 hazards + 2 vulnerabilities) in two rows below the main statistic
    """

    # --- Inline CSS/JS for hover-based <details> toggling and arrow rotation ---
    style_and_script = """
    <style>
    .hover-details summary::-webkit-details-marker {
      display: none; /* Hide the default triangle marker in Chrome/Safari */
    }
    .hover-details .dropdown-icon {
      transition: transform 0.3s;
    }
    .hover-details[open] .dropdown-icon {
      transform: rotate(180deg);
    }
    </style>
    <script>
    document.addEventListener('DOMContentLoaded', function() {
      // Convert <details.hover-details> to hover-based open/close
      document.querySelectorAll('.hover-details').forEach(function(el) {
        let summary = el.querySelector('summary');
        // Prevent default click toggle
        if (summary) {
          summary.addEventListener('click', function(e) {
            e.preventDefault();
          });
        }
        el.addEventListener('mouseenter', function() {
          el.setAttribute('open', '');
        });
        el.addEventListener('mouseleave', function() {
          el.removeAttribute('open');
        });
      });
    });
    </script>
    """

    # --- Park name ---
    park_name = properties.get("signname", "Unknown Park")

    # --- Estimated Investments ---
    raw_total = properties.get("EstInvTotal", "0")
    formatted_total = format_est_inv_total(raw_total)

    # --- Prepare Capital Projects (for the nested hover dropdown) ---
    project_columns = {}
    for f in PROJECT_FIELDS:
        project_columns[f] = parse_splitted(properties, f)
    num_projects = max(len(lst) for lst in project_columns.values()) if project_columns else 0

    # Clean up Titles by removing park name
    if "Title" in project_columns:
        project_columns["Title"] = [
            remove_signname_from_title(t, park_name) for t in project_columns["Title"]
        ]

    if num_projects > 0:
        # Build a small table for projects
        table_html = []
        table_html.append("<table style='width:100%; border-collapse:collapse; font-size:12px;'>")
        table_html.append("<thead><tr>")
        for col_name in PROJECT_FIELDS:
            alias = FIELD_ALIASES.get(col_name, col_name)
            table_html.append(f"<th style='background:#eee; padding:4px; font-weight:bold;'>{alias}</th>")
        table_html.append("</tr></thead><tbody>")
        for i in range(num_projects):
            table_html.append("<tr>")
            for col_name in PROJECT_FIELDS:
                val = project_columns[col_name][i] if i < len(project_columns[col_name]) else ""
                table_html.append(f"<td style='padding:4px;'>{val}</td>")
            table_html.append("</tr>")
        table_html.append("</tbody></table>")
        projects_content = "".join(table_html)
    else:
        projects_content = "<p style='margin:0;'>No recent capital projects.</p>"

    # --- "Estimated Investments" bubble, with nested hover-details for Projects ---
    investments_bubble = f"""
    <div style="
        border:1px solid #ddd; 
        background-color:#f0f0f0; 
        border-radius:12px; 
        margin-bottom:10px; 
        box-shadow: 0 2px 4px rgba(0,0,0,0.1); 
        padding:10px;">
      <p style="margin:0; font-size:16px;">
        <span style="font-weight:normal;">Estimated Recent Investments:</span><br>
        <span style="font-weight:bold;">${formatted_total}</span> (since {cutoff_date_simple})
      </p>

      <!-- Nested hover-to-open dropdown for Recent Capital Projects -->
      <details class="hover-details" style="
          border:1px solid #ccc;
          border-radius:8px;
          margin-top:10px;
          background-color:#eaeaea;
          overflow:hidden;">
        <summary style="
            font-size:14px; 
            cursor:pointer; 
            padding:8px; 
            margin:0; 
            display:flex; 
            justify-content:space-between; 
            align-items:center;">
          <span>Recent Capital Projects</span>
          <span class="dropdown-icon" style="font-weight:bold;">▼</span>
        </summary>
        <div style="padding:8px;">
          {projects_content}
        </div>
      </details>
    </div>
    """

    # --- Climate Risk Factor (non-collapsible bubble) ---
    climate_risk = properties.get("ClimateRiskFactor", "N/A")

    # Helper to build a table for hazard factors only
    def build_hazard_table(properties):
        rows = []
        for fk in HAZARD_FACTORS:
            info = DATASET_INFO.get(fk, {})
            alias = info.get("alias", "N/A")
            raw_field = info.get("raw", "")
            name = info.get("name", fk)
            suffix = info.get("suffix", "")

            raw_val = properties.get(raw_field, "N/A")
            idx_val = properties.get(alias, "N/A")

            # Format numeric
            if isinstance(raw_val, (int, float)):
                raw_val = f"{raw_val:.2f}{suffix}"
            if isinstance(idx_val, (int, float)):
                idx_val = f"{idx_val:.2f}"

            # Main row
            rows.append(f"""
            <tr>
              <td style="padding:4px; font-weight:bold;">{name}</td>
              <td style="padding:4px;">{raw_val}</td>
              <td style="padding:4px;">{idx_val}</td>
            </tr>
            """)

            # Sub‐indices for each hazard factor
            sublist = HAZARD_SUBINDICES.get(fk, [])
            for (sub_label, sub_key) in sublist:
                val = properties.get(sub_key, "N/A")
                if isinstance(val, (int, float)):
                    val = f"{val:.2f}"
                rows.append(f"""
                <tr>
                  <td style="padding:4px; padding-left:20px;">{sub_label}</td>
                  <td style="padding:4px;">{val}</td>
                  <td style="padding:4px;">N/A</td>
                </tr>
                """)

        return f"""
        <table style="width:100%; border-collapse:collapse; font-size:12px;">
          <thead>
            <tr>
              <th style="background:#eee; padding:4px;">Hazard Factor</th>
              <th style="background:#eee; padding:4px;">Raw Value</th>
              <th style="background:#eee; padding:4px;">Index Value</th>
            </tr>
          </thead>
          <tbody>
            {''.join(rows)}
          </tbody>
        </table>
        """

    hazard_table_html = build_hazard_table(properties)

    hazard_dropdown = f"""
    <details class="hover-details" style="
        border:1px solid #ccc;
        border-radius:8px;
        margin-top:10px;
        background-color:#eaeaea;
        overflow:hidden;">
      <summary style="
          font-size:14px; 
          cursor:pointer; 
          padding:8px; 
          margin:0; 
          display:flex; 
          justify-content:space-between; 
          align-items:center;">
        <span>Hazard</span>
        <span class="dropdown-icon" style="font-weight:bold;">▼</span>
      </summary>
      <div style="padding:8px;">
        {hazard_table_html}
      </div>
    </details>
    """

    # The five icons below the main Climate Risk Factor statistic, 3 on top row, 2 on bottom row
    icons_html = f"""
    <div style="margin-top:10px;">
      <!-- Top row: Heat Hazard, Coastal Flood Hazard, Stormwater Flood Hazard -->
      <div style="display: flex; justify-content: center; gap: 20px; margin-bottom: 10px;">
        <div style="text-align:center;">
          <img src="{ICONS_DIR}/{INDEX_ICONS['Heat Hazard']}" style="width:50px; height:auto;" />
          <div style="font-size:12px; margin-top:4px;">Heat Hazard</div>
        </div>
        <div style="text-align:center;">
          <img src="{ICONS_DIR}/{INDEX_ICONS['Coastal Flood Hazard']}" style="width:50px; height:auto;" />
          <div style="font-size:12px; margin-top:4px;">Coastal Flood Hazard</div>
        </div>
        <div style="text-align:center;">
          <img src="{ICONS_DIR}/{INDEX_ICONS['Stormwater Flood Hazard']}" style="width:50px; height:auto;" />
          <div style="font-size:12px; margin-top:4px;">Stormwater Flood Hazard</div>
        </div>
      </div>
      <!-- Bottom row: Heat Vulnerability, Flood Vulnerability -->
      <div style="display: flex; justify-content: center; gap: 20px;">
        <div style="text-align:center;">
          <img src="{ICONS_DIR}/{INDEX_ICONS['Heat Vulnerability']}" style="width:50px; height:auto;" />
          <div style="font-size:12px; margin-top:4px;">Heat Vulnerability</div>
        </div>
        <div style="text-align:center;">
          <img src="{ICONS_DIR}/{INDEX_ICONS['Flood Vulnerability']}" style="width:50px; height:auto;" />
          <div style="font-size:12px; margin-top:4px;">Flood Vulnerability</div>
        </div>
      </div>
    </div>
    """

    climate_bubble = f"""
    <div style="
        border:1px solid #ddd; 
        background-color:#f0f0f0; 
        border-radius:12px; 
        margin-bottom:10px; 
        box-shadow: 0 2px 4px rgba(0,0,0,0.1); 
        padding:10px;">
      <p style="margin:0; font-size:16px;">
        <span style="font-weight:normal;">Climate Risk Factor:</span><br>
        <span style="font-weight:bold;">{climate_risk}</span>
      </p>
      <!-- Only Hazard dropdown is shown; no Vulnerability dropdown -->
      {hazard_dropdown}
      {icons_html}
    </div>
    """

    # --- Combine everything ---
    html = []
    html.append(style_and_script)  # Inline <style> + <script> for hover-based details
    html.append(f'<h3 style="margin-bottom:0.2em; font-size:22px; font-weight:bold;">{park_name}</h3>')
    html.append(investments_bubble)
    html.append(climate_bubble)

    return "".join(html)

def style_function(feature):
    return {
        "fillColor": DATASET_INFO["Webmap"]["NYC_Parks"]["hex"],
        "color": DATASET_INFO["Webmap"]["NYC_Parks"]["hex"],
        "weight": 2,
        "fillOpacity": 0.6,
    }

def hex_to_rgba(hex_color, alpha=255):
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 6:
        r, g, b = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    elif len(hex_color) == 3:
        r, g, b = tuple(int(hex_color[i]*2, 16) for i in range(3))
    else:
        r, g, b = (0, 0, 0)
    return (r, g, b, alpha)

def raster_to_png(input_raster, output_png, colormap=None, raster_type=None):
    import numpy as np
    from PIL import Image
    import rasterio
    from config import DATASET_INFO

    with rasterio.open(input_raster) as src:
        data = src.read(1)
        nodata = src.nodata
    if nodata is not None:
        mask = (data != nodata)
    else:
        mask = (data != 0)
    height, width = data.shape
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    
    if colormap == "heat":
        if data[mask].size > 0:
            if data.max() > 200:  # likely in Kelvin
                data_f = (data - 273.15) * 9/5 + 32
            else:
                data_f = data
            min_temp = np.percentile(data_f[mask], 1)
            max_temp = np.percentile(data_f[mask], 99)
            norm = np.clip((data_f - min_temp) / (max_temp - min_temp), 0, 1)
            for i in range(height):
                for j in range(width):
                    if mask[i, j]:
                        t = norm[i, j]
                        if t < 0.33:
                            rgba[i, j, :] = [0, int(255 * t * 3), int(255 * (0.33 + t * 2)), 200]
                        elif t < 0.66:
                            rgba[i, j, :] = [int(255 * (t - 0.33) * 3), 255, int(255 * (1 - (t - 0.33) * 3)), 200]
                        else:
                            rgba[i, j, :] = [255, int(255 * (1 - (t - 0.66) * 3)), 0, 200]
    elif colormap == "flood":
        if raster_type == "FEMA":
            color_1pct = hex_to_rgba(DATASET_INFO["Webmap"]["FEMA_FloodHaz"]["hex_1pct"], alpha=200)
            color_0_2pct = hex_to_rgba(DATASET_INFO["Webmap"]["FEMA_FloodHaz"]["hex_0_2pct"], alpha=200)
            for i in range(height):
                for j in range(width):
                    if mask[i, j]:
                        val = data[i, j]
                        if val == 1:
                            rgba[i, j, :] = color_1pct
                        elif val == 2:
                            rgba[i, j, :] = color_0_2pct
                        else:
                            rgba[i, j, 3] = 0
        elif raster_type == "Stormwater":
            storm_hex = DATASET_INFO["Webmap"]["2080_Stormwater"]["hex"]
            shallow_alpha = DATASET_INFO["Webmap"]["2080_Stormwater"].get("shallow_alpha", 0.5)
            base_color = hex_to_rgba(storm_hex, alpha=255)
            for i in range(height):
                for j in range(width):
                    if mask[i, j]:
                        val = data[i, j]
                        if val == 1:
                            alpha = int(shallow_alpha * 255)
                        elif val == 2:
                            alpha = int(0.7 * 255)
                        elif val == 3:
                            alpha = int(0.9 * 255)
                        else:
                            alpha = 0
                        rgba[i, j, :] = [base_color[0], base_color[1], base_color[2], alpha]
        else:
            if data[mask].size > 0:
                if data[mask].max() > data[mask].min():
                    norm = np.clip((data - data[mask].min()) / (data[mask].max() - data[mask].min()), 0, 1)
                else:
                    norm = np.zeros_like(data)
                for i in range(height):
                    for j in range(width):
                        if mask[i, j] and norm[i, j] > 0:
                            rgba[i, j, 0] = 0
                            rgba[i, j, 1] = int(100 + 155 * norm[i, j])
                            rgba[i, j, 2] = int(255 - 100 * norm[i, j])
                            rgba[i, j, 3] = int(100 + 155 * norm[i, j])
    else:
        for i in range(height):
            for j in range(width):
                if mask[i, j]:
                    val = int(255 * (data[i, j] / data.max()))
                    rgba[i, j, :] = [val, val, val, 255]
    
    img = Image.fromarray(rgba)
    img.save(output_png)
    return output_png

def style_function(feature):
    return {
        "fillColor": DATASET_INFO["Webmap"]["NYC_Parks"]["hex"],
        "color": DATASET_INFO["Webmap"]["NYC_Parks"]["hex"],
        "weight": 2,
        "fillOpacity": 0.6,
    }

def generate_webmap():
    # Create a folium map with CartoDB Positron basemap
    m = folium.Map(location=[40.7128, -74.0060], zoom_start=10, tiles='CartoDB Positron')
    
    # Create web directory for processed rasters
    web_dir = os.path.join(OUTPUT_DIR, "web_layers")
    os.makedirs(web_dir, exist_ok=True)
    
    # Create downsampled versions of rasters
    scale_factor = 0.1  # Using 10% of original size
    
    heat_small = os.path.join(web_dir, "heat_small.tif")
    heat_png = os.path.join(web_dir, "heat.png")
    if not os.path.exists(heat_png):
        create_downsampled_raster(HEAT_FILE, heat_small, scale_factor)
        raster_to_png(heat_small, heat_png, colormap="heat")
    
    fema_small = os.path.join(web_dir, "fema_small.tif")
    fema_png = os.path.join(web_dir, "fema.png")
    if not os.path.exists(fema_png):
        create_downsampled_raster(FEMA_RASTER, fema_small, scale_factor)
        raster_to_png(fema_small, fema_png, colormap="flood", raster_type="FEMA")
    
    storm_small = os.path.join(web_dir, "storm_small.tif")
    storm_png = os.path.join(web_dir, "storm.png")
    if not os.path.exists(storm_png):
        create_downsampled_raster(STORM_RASTER, storm_small, scale_factor)
        raster_to_png(storm_small, storm_png, colormap="flood", raster_type="Stormwater")
    
    gdf = gpd.read_file(OUTPUT_GEOJSON)
    gdf = gdf.to_crs(epsg=4326)
    gdf["popup_html"] = gdf.apply(lambda row: generate_feature_html(row.to_dict()), axis=1)
    
    minx, miny, maxx, maxy = gdf.total_bounds
    nyc_bounds = [[miny, minx], [maxy, maxx]]
    
    geojson_data = gdf.to_json()
    
    popup = folium.GeoJsonPopup(
        fields=["popup_html"],
        aliases=[None],
        labels=False,
        parse_html=True,
        localize=True
    )
    
    folium.GeoJson(
        data=geojson_data,
        name="NYC Parks",
        style_function=style_function,
        popup=popup
    ).add_to(m)
    
    heat_config = DATASET_INFO["Webmap"]["Summer_Temperature"]
    folium.raster_layers.ImageOverlay(
        image=heat_png,
        bounds=nyc_bounds,
        name=heat_config["name"],
        opacity=0.7,
        show=False
    ).add_to(m)
    
    fema_config = DATASET_INFO["Webmap"]["FEMA_FloodHaz"]
    folium.raster_layers.ImageOverlay(
        image=fema_png,
        bounds=nyc_bounds,
        name=fema_config["name"],
        opacity=0.6,
        show=False
    ).add_to(m)
    
    storm_config = DATASET_INFO["Webmap"]["2080_Stormwater"]
    folium.raster_layers.ImageOverlay(
        image=storm_png,
        bounds=nyc_bounds,
        name=storm_config["name"],
        opacity=0.6,
        show=False
    ).add_to(m)
    
    folium.LayerControl().add_to(m)
    
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
    legend_html = """
    <div id="heat-legend" style="display:none; position: fixed; bottom: 50px; right: 50px; z-index:9999; background: white; padding: 10px; border: 1px solid grey; border-radius: 5px;">
        <h4 style="margin-top:0;">Summer Temperature</h4>
        <div style="display: flex; align-items: center; margin-bottom: 5px;">
            <div style="width: 200px; height: 20px; background: linear-gradient(to right, #0088ff, #00ffff, #ffff00, #ff0000);"></div>
        </div>
        <div style="display: flex; justify-content: space-between; width: 200px;">
            <span>Cooler</span>
            <span>Warmer</span>
        </div>
    </div>
    
    <div id="flood-legend" style="display:none; position: fixed; bottom: 50px; right: 50px; z-index:9999; background: white; padding: 10px; border: 1px solid grey; border-radius: 5px;">
        <h4 style="margin-top:0;">Flood Hazard</h4>
        <div style="display: flex; align-items: center; margin-bottom: 5px;">
            <div style="width: 200px; height: 20px; background: linear-gradient(to right, rgba(0,150,255,0.3), rgba(0,150,255,1));"></div>
        </div>
        <div style="display: flex; justify-content: space-between; width: 200px;">
            <span>Low</span>
            <span>High</span>
        </div>
    </div>
    """
    
    legend_script = """
    <script>
        var map = document.querySelector('.folium-map').map;
        var heatLayer = document.querySelector('img[alt="Summer Temperature"]');
        var femaLayer = document.querySelector('img[alt="FEMA Floodmap"]');
        var stormLayer = document.querySelector('img[alt="2080 Stormwater Flooding"]');
        var heatLegend = document.getElementById('heat-legend');
        var floodLegend = document.getElementById('flood-legend');
        
        function updateLegends() {
            if (heatLayer && map.hasLayer(heatLayer._layer) && 
                window.getComputedStyle(heatLayer).opacity > 0 && 
                window.getComputedStyle(heatLayer).display !== 'none') {
                heatLegend.style.display = 'block';
                floodLegend.style.display = 'none';
            }
            else if ((femaLayer && map.hasLayer(femaLayer._layer) && 
                      window.getComputedStyle(femaLayer).opacity > 0 && 
                      window.getComputedStyle(femaLayer).display !== 'none') ||
                     (stormLayer && map.hasLayer(stormLayer._layer) && 
                      window.getComputedStyle(stormLayer).opacity > 0 && 
                      window.getComputedStyle(stormLayer).display !== 'none')) {
                floodLegend.style.display = 'block';
                heatLegend.style.display = 'none';
            }
            else {
                heatLegend.style.display = 'none';
                floodLegend.style.display = 'none';
            }
        }
        
        map.on('overlayadd', updateLegends);
        map.on('overlayremove', updateLegends);
        map.on('zoomend', updateLegends);
        setTimeout(updateLegends, 1000);
    </script>
    """
    
    m.get_root().html.add_child(folium.Element(legend_html + legend_script))
    m.save(OUTPUT_WEBMAP)
    print("Webmap generated and saved to:", OUTPUT_WEBMAP)

if __name__ == "__main__":
    generate_webmap()