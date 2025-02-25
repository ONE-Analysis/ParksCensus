import folium
import geopandas as gpd
import json
import os
import re
from datetime import datetime

from config import OUTPUT_GEOJSON, OUTPUT_WEBMAP, DATASET_INFO, CUTOFF_DATE, cutoff_date_simple

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

def generate_feature_html(properties):
    """
    Builds an HTML string for the popup, including:
      - Park Name (signname)
      - EstInvTotal with formatting and line break
      - A capital projects table with the columns in PROJECT_FIELDS (one row per project),
        using FIELD_ALIASES for column headers, and removing signname from Title.
      - A left-justified Hazard & Vulnerability table for HAZARD_FIELDS
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

    # Hazard & Vulnerability table (left-justified)
    hazard_rows = []
    for hf in HAZARD_FIELDS:
        if hf in properties:
            hazard_val = properties[hf]
            hazard_rows.append((hf, hazard_val))
    if hazard_rows:
        html.append("<h4 style='margin-top:1em;'>Hazard & Vulnerability</h4>")
        html.append("<table border='1' style='border-collapse:collapse; font-size:12px; text-align:left;'>")
        for (hf_name, hf_val) in hazard_rows:
            html.append("<tr>")
            html.append(f"<th style='background:#f0f0f0; padding:4px;'>{hf_name}</th>")
            html.append(f"<td style='padding:4px;'>{hf_val}</td>")
            html.append("</tr>")
        html.append("</table>")

    return "".join(html)

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
    
    # Load the output GeoJSON with GeoPandas and reproject to EPSG:4326 (WGS84)
    gdf = gpd.read_file(OUTPUT_GEOJSON)
    gdf = gdf.to_crs(epsg=4326)
    
    # Add a column "popup_html" for each park
    gdf["popup_html"] = gdf.apply(lambda row: generate_feature_html(row.to_dict()), axis=1)
    
    # Convert to GeoJSON text
    geojson_data = gdf.to_json()
    
    # Create a folium GeoJsonPopup referencing the "popup_html" field
    popup = folium.GeoJsonPopup(
        fields=["popup_html"],  # only need our HTML field
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
    
    folium.LayerControl().add_to(m)
    m.save(OUTPUT_WEBMAP)
    print("Webmap generated and saved to:", OUTPUT_WEBMAP)

if __name__ == "__main__":
    generate_webmap()