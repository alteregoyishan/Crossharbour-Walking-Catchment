"""Step 15: apply one water/bridge policy and audit A0/A2/B2/C1/C2."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.catchment_barrier_review import run_barrier_consistent_review


if __name__ == "__main__":
    try:
        run_barrier_consistent_review()
    except Exception as exc:
        raise SystemExit(f"ERROR: Barrier-consistent catchment review stopped: {exc}")
