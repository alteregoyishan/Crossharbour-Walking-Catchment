"""Repository-relative paths; independent of the working directory."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"
EXTERNAL = DATA / "external"
PROCESSED = DATA / "processed"
OPENSTREETMAP = PROCESSED / "OpenStreetMap"
BASE_PEDESTRIAN_GRAPH = OPENSTREETMAP / "pedestrian_network.graphml"
REVIEWED_PEDESTRIAN_GRAPH = OPENSTREETMAP / "pedestrian_network_reviewed.graphml"
QA = DATA / "qa"
CLEANED = DATA / "cleaned"
OUTPUTS = ROOT / "outputs"


def ensure_directories() -> None:
    """Create writable result and cache directories, never modify raw data."""
    for path in [PROCESSED, OPENSTREETMAP, QA, CLEANED,
                 *(EXTERNAL / name for name in ("osm", "ons", "google")),
                 *(OUTPUTS / name for name in ("spatial", "tables", "figures", "logs"))]:
        path.mkdir(parents=True, exist_ok=True)
