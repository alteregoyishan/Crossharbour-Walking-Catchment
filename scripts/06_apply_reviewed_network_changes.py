"""Step 06: apply user-approved connections to a copy of the OSM graph."""
import logging
from pathlib import Path
import sys

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.network_changes import apply_approved_network_changes
from src.paths import ensure_directories
from src.utils import configure_logging


def main() -> int:
    ensure_directories()
    configure_logging()
    try:
        result = apply_approved_network_changes(load_config())
        for _, row in result["changes"].iterrows():
            logging.info(
                "%s: connected %s to %s with %.2f m reviewed connector",
                row["change_id"], row["source_node"], row["target_edge"],
                row["connector_length_m"],
            )
        logging.info(
            "Weak components: %d before; %d after",
            result["initial_components"], result["final_components"],
        )
        logging.info("Reviewed GraphML: %s", result["reviewed_graph"])
        logging.info("Reviewed GIS network: %s", result["reviewed_gpkg"])
        logging.info("Approved change log: %s", result["changes_output"])
        return 0
    except (FileNotFoundError, ValueError, TypeError, RuntimeError, OSError,
            nx.NetworkXError) as exc:
        logging.error("Applying reviewed network changes stopped: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
