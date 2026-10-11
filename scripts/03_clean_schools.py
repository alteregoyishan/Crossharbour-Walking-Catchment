"""Step 03: filter schools, run OS Places and resolve low confidence with Google."""
import logging
from pathlib import Path
import requests
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.paths import ensure_directories
from src.school_cleaning import clean_schools
from src.utils import configure_logging


def main() -> int:
    ensure_directories()
    configure_logging()
    try:
        result = clean_schools(load_config())
        logging.info(
            "London schools: %d source -> %d cleaned; %d closed, %d duplicate and %d overseas schools excluded before geocoding",
            result["source_count"], result["cleaned_count"],
            result["closed_excluded"], result["duplicates_removed"], result["overseas_excluded"],
        )
        logging.info(
            "OS Places: %d queries; %d matched; %d accepted directly",
            result["api_lookups"], result["matched_count"],
            result["os_accepted_count"],
        )
        logging.info(
            "Google: %d low-confidence queries; %d Google coordinates used; %d still require review",
            result["google_lookups"], result["google_used_count"], result["review_count"],
        )
        logging.info("Cleaned CSV: %s", result["csv_output"])
        logging.info("Cleaned GeoPackage: %s", result["gpkg_output"])
        logging.info("School QA table: %s", result["qa_output"])
        logging.info("Cleaning log: %s", result["cleaning_log"])
        logging.info("All OS matches: %s", result["match_audit"])
        logging.info("Manual review: %s", result["coordinate_review"])
        logging.info("Google validation: %s", result["google_validation"])
        return 0
    except (ValueError, FileNotFoundError, RuntimeError, requests.RequestException) as exc:
        logging.error("School cleaning stopped: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
