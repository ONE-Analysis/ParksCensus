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
    OUTPUT_GEOJSON, OUTPUT_WEBMAP, DATASET_INFO, 
    CUTOFF_DATE, cutoff_date_simple, 
    HEAT_FILE, FEMA_RASTER, STORM_RASTER, OUTPUT_DIR
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
    Builds an HTML string for the popup, including:
      - Park Name (signname)
      - EstInvTotal with formatting and line break
      - A capital projects table with the columns in PROJECT_FIELDS (one row per project),
        using FIELD_ALIASES for column headers, and removing signname from Title.
      - A comprehensive Hazard & Vulnerability table showing all raw and index values
      - The Capital Projects table is scrollable if > 2 rows.
    """
    # Park name
    park_name = properties.get("signname", "Unknown Park")

    # Format the total investment with commas
    raw_total = properties.get("EstInvTotal", "0")
    formatted_total = format_est_inv_total(raw_total)

    # Prepare lists for each project column
    project_columns = {}
    for f in PROJECT_FIELDS:
        project_columns[f] = parse_splitted(properties, f)

    # Determine how many projects (max length across all columns)
    num_projects = max(len(lst) for lst in project_columns.values()) if project_columns else 0

    # Remove signname from Title entries
    if "Title" in project_columns:
        project_columns["Title"] = [
            remove_signname_from_title(t, park_name) for t in project_columns["Title"]
        ]

    # Build the HTML
    html = []
    # Park name as a heading
    html.append(f"<h3 style='margin-bottom:0.2em;'>{park_name}</h3>")
    # Estimated Recent Investment on a new line, with cutoff date
    html.append(
        f"<p style='margin-top:0;'>Estimated Recent Investment:<br>"
        f"<b>${formatted_total}</b> (since {cutoff_date_simple})</p>"
    )

    # Capital Projects table
    if num_projects > 0:
        html.append("<h4 style='margin-top:1em;'>Capital Projects</h4>")

        # If more than 2 projects, wrap the table in a scrollable container
        if num_projects > 2:
            html.append("<div style='max-height:200px; overflow-y:auto;'>")

        html.append("<table border='1' style='border-collapse:collapse; font-size:12px; text-align:left;'>")
        # Table header
        html.append("<thead><tr>")
        for col_name in PROJECT_FIELDS:
            alias = FIELD_ALIASES.get(col_name, col_name)
            html.append(f"<th style='background:#f0f0f0; padding:4px;'>{alias}</th>")
        html.append("</tr></thead>")
        html.append("<tbody>")
        # One row per project
        for i in range(num_projects):
            html.append("<tr>")
            for col_name in PROJECT_FIELDS:
                col_values = project_columns[col_name]
                val = col_values[i] if i < len(col_values) else ""
                html.append(f"<td style='padding:4px;'>{val}</td>")
            html.append("</tr>")
        html.append("</tbody></table>")

        if num_projects > 2:
            html.append("</div>")  # close scrollable container
    else:
        html.append("<p>No recent capital projects.</p>")

    # Enhanced Hazard & Vulnerability table with ALL raw and index values
    html.append("<h4 style='margin-top:1em;'>Hazard & Vulnerability</h4>")
    
    # Main indices table
    html.append("<table border='1' style='border-collapse:collapse; font-size:12px; text-align:left; width:100%;'>")
    html.append("<thead><tr>")
    html.append("<th style='background:#f0f0f0; padding:4px;'>Index</th>")
    html.append("<th style='background:#f0f0f0; padding:4px;'>Raw Value</th>")
    html.append("<th style='background:#f0f0f0; padding:4px;'>Index Value</th>")
    html.append("</tr></thead>")
    html.append("<tbody>")
    
    # Define index to dataset mapping
    index_to_dataset = {
        "HeatHaz": "Heat_Hazard_Index",
        "CoastalFloodHaz": "Coastal_Flood_Hazard_Index",
        "StormFloodHaz": "Stormwater_Flood_Hazard_Index",
        "HeatVuln": "Heat_Vulnerability_Index",
        "FloodVuln": "Flood_Vulnerability_Index"
    }
    
    # Add a row for each main hazard/vulnerability index
    for index_field in HAZARD_FIELDS:
        if index_field in properties:
            # Get the dataset information
            dataset_key = index_to_dataset.get(index_field)
            if dataset_key in DATASET_INFO:
                dataset = DATASET_INFO[dataset_key]
                raw_field = dataset.get("raw")
                raw_value = properties.get(raw_field, "N/A")
                index_value = properties.get(index_field, "N/A")
                
                # Format raw values with appropriate precision
                if isinstance(raw_value, (int, float)):
                    if dataset_key == "Heat_Hazard_Index":
                        raw_value = f"{raw_value:.1f}°F"
                    else:
                        raw_value = f"{raw_value:.2f}"
                
                # Format index values
                if isinstance(index_value, (int, float)):
                    index_value = f"{index_value:.2f}"
                
                # Add row to table
                html.append("<tr>")
                html.append(f"<td style='padding:4px;'>{dataset.get('name', index_field)}</td>")
                html.append(f"<td style='padding:4px;'>{raw_value}</td>")
                html.append(f"<td style='padding:4px;'>{index_value}</td>")
                html.append("</tr>")
    
    html.append("</tbody></table>")
    
    # Add a separate detailed table for all flood components
    html.append("<h5 style='margin-top:1em;'>Detailed Flood Components</h5>")
    html.append("<div style='max-height:300px; overflow-y:auto;'>")
    html.append("<table border='1' style='border-collapse:collapse; font-size:12px; text-align:left; width:100%;'>")
    html.append("<thead><tr>")
    html.append("<th style='background:#f0f0f0; padding:4px;'>Component</th>")
    html.append("<th style='background:#f0f0f0; padding:4px;'>Within Site (%)</th>")
    html.append("<th style='background:#f0f0f0; padding:4px;'>Nearby (%)</th>")
    html.append("</tr></thead>")
    html.append("<tbody>")
    
    # Coastal Flood Components
    html.append("<tr>")
    html.append("<td colspan='3' style='background:#e0e0e0; padding:4px; font-weight:bold;'>Coastal Flood Components</td>")
    html.append("</tr>")
    
    # 500-year Coastal Flood
    in_val = properties.get("Cst_500_in", "N/A")
    nr_val = properties.get("Cst_500_nr", "N/A")
    if isinstance(in_val, (int, float)):
        in_val = f"{in_val * 100:.1f}%"
    if isinstance(nr_val, (int, float)):
        nr_val = f"{nr_val * 100:.1f}%"
    html.append("<tr>")
    html.append("<td style='padding:4px;'>500-year Coastal Flood</td>")
    html.append(f"<td style='padding:4px;'>{in_val}</td>")
    html.append(f"<td style='padding:4px;'>{nr_val}</td>")
    html.append("</tr>")
    
    # 100-year Coastal Flood
    in_val = properties.get("Cst_100_in", "N/A")
    nr_val = properties.get("Cst_100_nr", "N/A")
    if isinstance(in_val, (int, float)):
        in_val = f"{in_val * 100:.1f}%"
    if isinstance(nr_val, (int, float)):
        nr_val = f"{nr_val * 100:.1f}%"
    html.append("<tr>")
    html.append("<td style='padding:4px;'>100-year Coastal Flood</td>")
    html.append(f"<td style='padding:4px;'>{in_val}</td>")
    html.append(f"<td style='padding:4px;'>{nr_val}</td>")
    html.append("</tr>")
    
    # Stormwater Components
    html.append("<tr>")
    html.append("<td colspan='3' style='background:#e0e0e0; padding:4px; font-weight:bold;'>Stormwater Components</td>")
    html.append("</tr>")
    
    # Shallow Stormwater
    in_val = properties.get("StrmShl_in", "N/A")
    nr_val = properties.get("StrmShl_nr", "N/A")
    if isinstance(in_val, (int, float)):
        in_val = f"{in_val * 100:.1f}%"
    if isinstance(nr_val, (int, float)):
        nr_val = f"{nr_val * 100:.1f}%"
    html.append("<tr>")
    html.append("<td style='padding:4px;'>Shallow Stormwater</td>")
    html.append(f"<td style='padding:4px;'>{in_val}</td>")
    html.append(f"<td style='padding:4px;'>{nr_val}</td>")
    html.append("</tr>")
    
    # Deep Stormwater
    in_val = properties.get("StrmDp_in", "N/A")
    nr_val = properties.get("StrmDp_nr", "N/A")
    if isinstance(in_val, (int, float)):
        in_val = f"{in_val * 100:.1f}%"
    if isinstance(nr_val, (int, float)):
        nr_val = f"{nr_val * 100:.1f}%"
    html.append("<tr>")
    html.append("<td style='padding:4px;'>Deep Stormwater</td>")
    html.append(f"<td style='padding:4px;'>{in_val}</td>")
    html.append(f"<td style='padding:4px;'>{nr_val}</td>")
    html.append("</tr>")
    
    # Tidal Stormwater
    in_val = properties.get("StrmTid_in", "N/A")
    nr_val = properties.get("StrmTid_nr", "N/A")
    if isinstance(in_val, (int, float)):
        in_val = f"{in_val * 100:.1f}%"
    if isinstance(nr_val, (int, float)):
        nr_val = f"{nr_val * 100:.1f}%"
    html.append("<tr>")
    html.append("<td style='padding:4px;'>Tidal Stormwater</td>")
    html.append(f"<td style='padding:4px;'>{in_val}</td>")
    html.append(f"<td style='padding:4px;'>{nr_val}</td>")
    html.append("</tr>")
    
    html.append("</tbody></table>")
    html.append("</div>")

    return "".join(html)

def style_function(feature):
    return {
        "fillColor": DATASET_INFO["Webmap"]["NYC_Parks"]["hex"],
        "color": DATASET_INFO["Webmap"]["NYC_Parks"]["hex"],
        "weight": 2,
        "fillOpacity": 0.6,
    }

def raster_to_png(input_raster, output_png, colormap=None):
    """
    Convert a single-band raster to a PNG image with appropriate colormap
    """
    import numpy as np
    from PIL import Image
    
    with rasterio.open(input_raster) as src:
        # Read the data
        data = src.read(1)
        nodata = src.nodata
        
        # Create a mask for valid data
        if nodata is not None:
            mask = (data != nodata)
        else:
            mask = (data != 0)  # Assuming 0 is nodata if not specified
        
        # Create an RGBA array (initialize with transparent)
        height, width = data.shape
        rgba = np.zeros((height, width, 4), dtype=np.uint8)
        
        # Apply colormap based on the type
        if colormap == "heat":
            # For temperature data - create a blue to red temperature map
            if data[mask].size > 0:  # Check if we have valid data
                # Kelvin to Fahrenheit conversion
                if data.max() > 200:  # Likely in Kelvin
                    data_f = (data - 273.15) * 9/5 + 32
                else:
                    data_f = data  # Assume already in appropriate scale
                
                # Min-max temperature range for normalization
                min_temp = np.percentile(data_f[mask], 1)  # 1st percentile to avoid outliers
                max_temp = np.percentile(data_f[mask], 99)  # 99th percentile to avoid outliers
                
                # Normalize to 0-1 scale
                norm = np.clip((data_f - min_temp) / (max_temp - min_temp), 0, 1)
                
                # Apply custom colormap (cool to hot)
                for i in range(height):
                    for j in range(width):
                        if mask[i, j]:
                            t = norm[i, j]
                            if t < 0.33:  # Cool (blue to cyan)
                                rgba[i, j, 0] = 0
                                rgba[i, j, 1] = int(255 * t * 3)
                                rgba[i, j, 2] = int(255 * (0.33 + t * 2))
                                rgba[i, j, 3] = 200  # Semi-transparent
                            elif t < 0.66:  # Moderate (cyan to yellow)
                                rgba[i, j, 0] = int(255 * (t - 0.33) * 3)
                                rgba[i, j, 1] = 255
                                rgba[i, j, 2] = int(255 * (1 - (t - 0.33) * 3))
                                rgba[i, j, 3] = 200
                            else:  # Hot (yellow to red)
                                rgba[i, j, 0] = 255
                                rgba[i, j, 1] = int(255 * (1 - (t - 0.66) * 3))
                                rgba[i, j, 2] = 0
                                rgba[i, j, 3] = 200
        
        elif colormap == "flood":
            # For flood data - blue colormap
            if data[mask].size > 0:
                # Normalize to 0-1 scale
                if data[mask].max() > data[mask].min():
                    norm = np.clip((data - data[mask].min()) / (data[mask].max() - data[mask].min()), 0, 1)
                else:
                    norm = np.zeros_like(data)
                
                # Apply blue colormap with higher opacity for higher values
                for i in range(height):
                    for j in range(width):
                        if mask[i, j] and norm[i, j] > 0:
                            rgba[i, j, 0] = 0
                            rgba[i, j, 1] = int(100 + 155 * norm[i, j])  # Light to dark blue
                            rgba[i, j, 2] = int(255 - 100 * norm[i, j])
                            rgba[i, j, 3] = int(100 + 155 * norm[i, j])  # More opaque for higher values
        
        # Create PIL image from RGBA array
        img = Image.fromarray(rgba)
        
        # Save as PNG
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
    
    # Process heat raster
    heat_small = os.path.join(web_dir, "heat_small.tif")
    heat_png = os.path.join(web_dir, "heat.png")
    if not os.path.exists(heat_png):
        create_downsampled_raster(HEAT_FILE, heat_small, scale_factor)
        raster_to_png(heat_small, heat_png, colormap="heat")
    
    # Process FEMA raster
    fema_small = os.path.join(web_dir, "fema_small.tif")
    fema_png = os.path.join(web_dir, "fema.png")
    if not os.path.exists(fema_png):
        create_downsampled_raster(FEMA_RASTER, fema_small, scale_factor)
        raster_to_png(fema_small, fema_png, colormap="flood")
    
    # Process storm raster
    storm_small = os.path.join(web_dir, "storm_small.tif")
    storm_png = os.path.join(web_dir, "storm.png")
    if not os.path.exists(storm_png):
        create_downsampled_raster(STORM_RASTER, storm_small, scale_factor)
        raster_to_png(storm_small, storm_png, colormap="flood")
    
    # Load the output GeoJSON with GeoPandas and reproject to EPSG:4326 (WGS84)
    gdf = gpd.read_file(OUTPUT_GEOJSON)
    gdf = gdf.to_crs(epsg=4326)
    
    # Add a column "popup_html" for each park
    gdf["popup_html"] = gdf.apply(lambda row: generate_feature_html(row.to_dict()), axis=1)
    
    # Calculate NYC bounds from parks data
    minx, miny, maxx, maxy = gdf.total_bounds
    nyc_bounds = [[miny, minx], [maxy, maxx]]
    
    # Convert to GeoJSON text
    geojson_data = gdf.to_json()
    
    # Create a folium GeoJsonPopup referencing the "popup_html" field
    popup = folium.GeoJsonPopup(
        fields=["popup_html"],
        aliases=[None],
        labels=False,
        parse_html=True,
        localize=True
    )
    
    # Create the GeoJson layer with style and that popup
    folium.GeoJson(
        data=geojson_data,
        name="NYC Parks",
        style_function=style_function,
        popup=popup
    ).add_to(m)
    
    # Add raster layers as image overlays
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
    
    # Add layer control
    folium.LayerControl().add_to(m)
    
    # Add JavaScript to show/hide layers based on zoom level
    min_zoom_level = 13
    script = f"""
    <script>
        var map = document.querySelector('.folium-map').map;
        var heatLayer = document.querySelector('img[alt="{heat_config["name"]}"]');
        var femaLayer = document.querySelector('img[alt="{fema_config["name"]}"]');
        var stormLayer = document.querySelector('img[alt="{storm_config["name"]}"]');
        
        // Set initial visibility based on zoom level
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
        
        // Update visibility on zoom change
        map.on('zoomend', updateVisibility);
        
        // Set initial visibility
        updateVisibility();
    </script>
    """
    
    # Add the JavaScript to the map
    m.get_root().html.add_child(folium.Element(script))
    # Add a legend for the heat layer
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
    
    # Add JavaScript to toggle the appropriate legend based on active layer
    legend_script = """
    <script>
        var map = document.querySelector('.folium-map').map;
        var heatLayer = document.querySelector('img[alt="Summer Temperature"]');
        var femaLayer = document.querySelector('img[alt="FEMA Floodmap"]');
        var stormLayer = document.querySelector('img[alt="2080 Stormwater Flooding"]');
        var heatLegend = document.getElementById('heat-legend');
        var floodLegend = document.getElementById('flood-legend');
        
        function updateLegends() {
            // Check if heat layer is visible
            if (heatLayer && map.hasLayer(heatLayer._layer) && 
                window.getComputedStyle(heatLayer).opacity > 0 && 
                window.getComputedStyle(heatLayer).display !== 'none') {
                heatLegend.style.display = 'block';
                floodLegend.style.display = 'none';
            }
            // Check if flood layers are visible
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
        
        // Update on layer add/remove
        map.on('overlayadd', updateLegends);
        map.on('overlayremove', updateLegends);
        
        // Update on zoom change (because we show/hide based on zoom)
        map.on('zoomend', updateLegends);
        
        // Initial update
        setTimeout(updateLegends, 1000);
    </script>
    """
    
    # Add the legend HTML and script
    m.get_root().html.add_child(folium.Element(legend_html + legend_script))

    m.save(OUTPUT_WEBMAP)
    print("Webmap generated and saved to:", OUTPUT_WEBMAP)

if __name__ == "__main__":
    generate_webmap()