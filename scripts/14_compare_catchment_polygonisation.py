"""Step 14: diagnose holes and compare 10-minute polygonisation methods."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.catchment_polygonisation import run_polygonisation_review


if __name__ == "__main__":
    try:
        run_polygonisation_review()
    except Exception as exc:
        raise SystemExit(f"ERROR: Catchment polygonisation review stopped: {exc}")
