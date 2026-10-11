"""Step 12: compare official Google walking isochrones with OSMnx outputs."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.google_isochrones import run_google_comparison


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true', help='request all six Google isochrones again')
    args = parser.parse_args()
    try:
        run_google_comparison(force=args.force)
    except Exception as exc:
        raise SystemExit(f'ERROR: Google isochrone comparison stopped: {exc}')
