"""Step 02: clean confirmed population duplicate and geometry issues."""
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.paths import ensure_directories
from src.population_cleaning import clean_population
from src.utils import configure_logging


def main() -> int:
    ensure_directories()
    configure_logging()
    try:
        result = clean_population()
        logging.info(
            "Population cleaned: %d -> %d records; %d duplicate removed; %d geometry repaired",
            result["source_count"], result["cleaned_count"],
            result["duplicates_removed"], result["geometries_repaired"],
        )
        logging.info("Cleaned data: %s", result["cleaned_output"])
        logging.info("Cleaning log: %s", result["cleaning_log"])
        return 0
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        logging.error("Population cleaning stopped: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
