"""Step 13: read-only OS NGD collection and small-sample discovery."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.os_ngd_discovery import run_discovery


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--buffer-m', type=float, default=350)
    parser.add_argument('--limit', type=int, default=10)
    args = parser.parse_args()
    try:
        run_discovery(args.buffer_m, args.limit)
    except Exception as exc:
        raise SystemExit(f'ERROR: OS NGD discovery stopped: {exc}')
