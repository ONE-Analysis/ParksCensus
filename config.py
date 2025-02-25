import os
from datetime import datetime

# Base directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(BASE_DIR, "input")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

# Input file paths
PARKS_FILE = os.path.join(INPUT_DIR, "NYC_Parks.geojson")
CAPITAL_PROJECTS_FILE = os.path.join(INPUT_DIR, "DPR_CapitalProjects.geojson")
HEAT_FILE = os.path.join(INPUT_DIR, "Landsat9_ThermalComposite_ST_B10_2020-2023.tif")
FEMA_RASTER = os.path.join(INPUT_DIR, "FEMA_FloodHaz_Raster.tif")
STORM_RASTER = os.path.join(INPUT_DIR, "Stormwater2080_Raster.tif")
HVI_DATA = os.path.join(INPUT_DIR, "HVI.geojson")
FVI_DATA = os.path.join(INPUT_DIR, "FVI.geojson")

# Output file paths
OUTPUT_GEOJSON = os.path.join(OUTPUT_DIR, "NYC_Parks_Census.geojson")
OUTPUT_WEBMAP = os.path.join(OUTPUT_DIR, "ParksCensus.html")

# Coordinate Reference System for all datasets
CRS = "EPSG:6539"

# Analysis parameters
ANALYSIS_BUFFER_FT = 2000       # buffer distance (in feet) for raster calculations
RESOLUTION = 30                 # target raster resolution

# Cutoff date for CapitalProjects (only include projects completed on/after this date)
cutoff_date_simple = "01/01/2020"

CUTOFF_DATE = datetime.strptime(f"{cutoff_date_simple} 12:00:00 AM", "%m/%d/%Y %I:%M:%S %p")

# Data dictionary for indices and webmap configuration
analysis_buffer_ft = ANALYSIS_BUFFER_FT  # used in text descriptions

DATASET_INFO = {
    "Heat_Hazard_Index": {
        "alias": "HeatHaz",
        "raw": "heat_mean",
        "name": "Heat Hazard",
        "description": f"Prioritizes parks in areas with higher summer temperatures, based on a buffer of {ANALYSIS_BUFFER_FT} feet. <br>Source:<br>Landsat Infrared Cloudless Composite (2020-2023)",
        "prefix": "",
        "suffix": " °F",
        "hex": "#C40A0A"
    },
    "Coastal_Flood_Hazard_Index": {
        "alias": "CoastalFloodHaz",
        "raw": "coastal_flood_risk",
        "name": "Coastal Flood Hazard",
        "description": f"Prioritizes parks with higher amounts of coastal flooding within {ANALYSIS_BUFFER_FT} feet. Parks with any direct overlap with coastal flooding are excluded. <br>Source:<br>FEMA Flood Maps",
        "prefix": "",
        "suffix": "",
        "hex": "#75E1FF"
    },
    "Stormwater_Flood_Hazard_Index": {
        "alias": "StormFloodHaz",
        "raw": "stormwater_flood_risk",
        "name": "Stormwater Flood Hazard",
        "description": f"Prioritizes parks with higher amounts of stormwater flooding within {ANALYSIS_BUFFER_FT} feet. <br>Source:<br>Stormwater Flood Maps, NYC DEP",
        "prefix": "",
        "suffix": "",
        "hex": "#244489"
    },
    "Heat_Vulnerability_Index": {
        "alias": "HeatVuln",
        "raw": "hvi_area",
        "name": "Heat Vulnerability",
        "description": f"Prioritizes parks in areas of higher social vulnerability to heat hazards, based on a buffer of {ANALYSIS_BUFFER_FT} feet. <br>Source:<br>NYC DOHMH Heat Vulnerability Index",
        "prefix": "",
        "suffix": "",
        "hex": "#C77851"
    },
    "Flood_Vulnerability_Index": {
        "alias": "FloodVuln",
        "raw": "flood_vuln",
        "name": "Flood Vulnerability",
        "description": f"Prioritizes parks in areas of higher social vulnerability to flood hazards, based on a buffer of {ANALYSIS_BUFFER_FT} feet. <br>Source:<br>NYC MOCEJ Flood Vulnerability Indices",
        "prefix": "",
        "suffix": "",
        "hex": "#6168C1"
    },
    "CapitalProjects": {
        # List of fields from CapitalProjects to concatenate (for popup display)
        "concat_fields": ["Title", "Summary", "CurrentPha", "DesignPerc", "Procuremen", "Constructi", "Construc_4", "ProjectLia", "EstInvestment", "FundingSou"],
        "est_total_field": "EstInvTotal"  # field name for summed investments
    },
    # Webmap layer configurations (colors, names, and opacities)
    "Webmap": {
        "2080_Stormwater": {"name": "2080 Stormwater Flooding", "hex": "#244489", "shallow_alpha": 0.5},
        "FEMA_FloodHaz": {"name": "FEMA Floodmap", "hex_1pct": "#75E1FF", "hex_0_2pct": "#BEE7FF"},
        "Summer_Temperature": {"name": "Summer Temperature", "color_ramp": {"start": "#C40A0A00", "end": "#C40A0A"}},
        "NYC_Parks": {"name": "NYC Parks", "hex": "#328232"}
    }
}