"""Step 07: select site origins from observed public footway crossings."""
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.paths import ensure_directories
from src.site_origins import build_site_origins
from src.utils import configure_logging


def main() -> int:
    ensure_directories()
    configure_logging()
    try:
        result = build_site_origins(load_config())
        logging.info(
            "Site origins: %d reviewed public-network contacts on the %s sides; max edge distance %.3f m",
            result["origin_count"], ", ".join(result["sides"]), result["max_point_to_edge_distance_m"],
        )
        logging.info("GIS origin layers: %s", result["output"])
        logging.info("Origin summary: %s", result["summary"])
        return 0
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        logging.error("Site origin selection stopped: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
