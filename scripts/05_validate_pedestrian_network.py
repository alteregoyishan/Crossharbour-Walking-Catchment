"""Step 05: validate the saved OSM pedestrian graph without modifying it."""
import argparse
import logging
from pathlib import Path
import sys

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.paths import ensure_directories, REVIEWED_PEDESTRIAN_GRAPH
from src.pedestrian_network_qa import run_pedestrian_network_qa
from src.utils import configure_logging


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reviewed", action="store_true",
        help="Validate the graph containing user-approved network changes",
    )
    args = parser.parse_args()
    ensure_directories()
    configure_logging()
    try:
        graph_path = REVIEWED_PEDESTRIAN_GRAPH if args.reviewed else None
        result = run_pedestrian_network_qa(load_config(), graph_path=graph_path)
        issues = result["issues"]
        for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            logging.info("Network QA %s findings: %d", severity,
                         int((issues["severity"] == severity).sum()))
        logging.info("QA source GraphML SHA-256 (unchanged during QA): %s", result["source_sha256"])
        logging.info("QA summary: %s", result["summary_output"])
        logging.info("Spatial issues: %s", result["issues_output"])
        logging.info("Local QA map: %s", result["map_output"])
        logging.info("Approved network changes: %s", result["changes_output"])
        logging.info("Restricted-link review checklist: %s", result["restricted_review_output"])
        logging.warning("Suitability: %s. %s", result["suitability"], result["recommendation"])
        return 0
    except (FileNotFoundError, ValueError, TypeError, RuntimeError, OSError,
            nx.NetworkXError) as exc:
        logging.error("Pedestrian network QA stopped: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
