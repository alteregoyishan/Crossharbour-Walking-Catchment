"""Logging and immutable source provenance."""
import hashlib
import logging
from .paths import RAW


def raw_hashes() -> dict[str, str]:
    """Hash every original file, including SQLite sidecars."""
    result = {}
    for path in sorted(RAW.iterdir()):
        if path.is_file() and path.name != ".DS_Store":
            h = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(block)
            result[path.name] = h.hexdigest()
    return result


def configure_logging() -> None:
    """Show concise progress in the terminal."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
