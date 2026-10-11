"""Step 08: insert reviewed origins into OSM edges and calculate catchments."""
import logging
from pathlib import Path
import sys

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.paths import ensure_directories
from src.utils import configure_logging
from src.walking_catchment import run_walking_catchments


def main() -> int:
    ensure_directories()
    configure_logging()
    try:
        result = run_walking_catchments(load_config())
        for _, row in result["origin_qa"].iterrows():
            logging.info(
                "Origin %s: edge %s; distance %.3f m; highway=%s; accessible=%s; status=%s",
                row["origin_id"], row["osmid"], row["point_to_edge_distance_m"],
                row["highway"], row["pedestrian_accessible"], row["status"],
            )
        logging.info("Routing graph: %d nodes; %d directed edges", result["node_count"], result["edge_count"])
        for _, row in result["summary"].iterrows():
            logging.info(
                "%d minutes: %.1f m reachable network; %.1f m2 (%.2f ha) buffered catchment",
                row["time_minutes"], row["reachable_network_length_m"],
                row["catchment_area_m2"], row["catchment_area_ha"],
            )
        logging.warning(result["warning"])
        logging.info("QA map: %s", result["qa_map"])
        return 0
    except (ValueError, TypeError, RuntimeError, OSError, nx.NetworkXError) as exc:
        logging.error("Walking catchment step stopped: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
