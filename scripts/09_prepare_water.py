"""Step 09: cache OS NGD water, with OSM as an explicit fallback."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.service_area_analysis import prepare_water

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', choices=['auto', 'os-ngd', 'osm'], default='auto',
                        help='auto follows WATER_SOURCE and defaults to the existing OSM water')
    parser.add_argument('--force', action='store_true', help='download the selected source again')
    args = parser.parse_args()
    try:
        prepare_water(args.source, args.force)
    except Exception as exc:
        raise SystemExit(f'ERROR: Water preparation stopped: {exc}')
