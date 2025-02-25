from analysis_modules import run_analysis
from webmap import generate_webmap
import os

def main():
    # Define the output file path (assumed to be the same as in run_analysis)
    OUTPUT_GEOJSON = "output/NYC_Parks_Census.geojson"
    
    # Check if the analysis output already exists
    if os.path.exists(OUTPUT_GEOJSON):
        print(f"Analysis output file {OUTPUT_GEOJSON} already exists. Skipping analysis.")
    else:
        print("Running analysis...")
        run_analysis()
    
    # Always generate the webmap
    generate_webmap()

if __name__ == "__main__":
    main()