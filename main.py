from analysis_modules import run_analysis
from webmap import generate_webmap
import os
import webbrowser

def main():
    # Define the output file path (assumed to be the same as in run_analysis)
    OUTPUT_GEOJSON = "output/NYC_Parks_Census.geojson"
    HTML_OUTPUT = "output/ParksCensus.html"
    
    # Check if the analysis output already exists
    if os.path.exists(OUTPUT_GEOJSON):
        print(f"Analysis output file {OUTPUT_GEOJSON} already exists. Skipping analysis.")
    else:
        print("Running analysis...")
        run_analysis()
    
    # Generate the webmap
    generate_webmap()
    
    # Use the known HTML file path from the output message
    html_file = os.path.join(os.getcwd(), HTML_OUTPUT)
    
    # Open the HTML file in the default web browser
    print(f"Opening {html_file} in web browser...")
    webbrowser.open('file://' + os.path.abspath(html_file))

if __name__ == "__main__":
    main()