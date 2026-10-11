"""Step 01: run data QA independently from any working directory."""
import argparse
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.data_validation import run_validation
from src.paths import ensure_directories
from src.utils import configure_logging


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Optional configuration file")
    args = parser.parse_args()
    ensure_directories()
    configure_logging()
    try:
        summary = run_validation(load_config(args.config))
        logging.info("QA complete: %d errors, %d warnings", summary["error_count"], summary["warning_count"])
        for category, count in sorted(summary["category_counts"].items()):
            logging.info("  %s: %d", category, count)
        logging.info("Issue details: %s", summary["issue_output"])
        logging.info("Grouped summary: %s", summary["summary_output"])
        return 0
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        logging.error("QA stopped: %s", exc)
        return 1

if __name__ == "__main__":
    sys.exit(main())
