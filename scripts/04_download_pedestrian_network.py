"""Step 04: download the OSM pedestrian network covering all cleaned schools."""
import argparse
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.paths import ensure_directories
from src.pedestrian_network import download_pedestrian_network
from src.utils import configure_logging


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Ignore an existing matching GraphML and query Overpass again")
    args = parser.parse_args()
    ensure_directories()
    configure_logging()
    try:
        result = download_pedestrian_network(load_config(), force_download=args.force)
        logging.info(
            "Pedestrian network: %d schools; %d nodes; %d directed edges; max school-to-node distance %.1f m",
            result["school_count"], result["node_count"], result["edge_count"],
            result["max_school_to_node_distance_m"],
        )
        logging.info("Reused existing GraphML: %s", result["reused_graphml"])
        logging.info("NetworkX GraphML: %s", result["graphml_output"])
        logging.info("GIS network: %s", result["gpkg_output"])
        logging.info("Download area: %s", result["download_area"])
        logging.info("School access QA: %s", result["school_access_qa"])
        return 0
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        logging.error("Pedestrian network step stopped: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
