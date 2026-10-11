"""Step 17: preserve P1 and test OS NGD-supported population allocation."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.population_sensitivity import run_population_sensitivity


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-download", action="store_true",
                        help="Refresh the bounded OS NGD support extract")
    args = parser.parse_args()
    try:
        run_population_sensitivity(force_download=args.force_download)
    except Exception as exc:
        raise SystemExit(f"ERROR: Population sensitivity stopped: {exc}")
