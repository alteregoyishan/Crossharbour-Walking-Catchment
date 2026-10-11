"""Step 16: export the selected C1 5/10/15-minute final map series."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.c1_final_maps import run_final_c1_maps


if __name__ == "__main__":
    try:
        run_final_c1_maps()
    except Exception as exc:
        raise SystemExit(f"ERROR: Final C1 map export stopped: {exc}")
