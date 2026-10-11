"""Build lightweight, commit-ready assets for the static GitHub Pages report."""
from __future__ import annotations

from pathlib import Path
import shutil

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
MAPS = ROOT / "outputs" / "maps"
SITE = ROOT / "site"
IMAGES = SITE / "assets" / "images"
DOWNLOADS = SITE / "assets" / "downloads"

IMAGE_SOURCES = {
    "figure_00a_site_origin_selection.png": "origin-selection.webp",
    "figure_00b_all_cleaned_schools.png": "schools-overview.webp",
    "figure_02_c1_10min.png": "catchment-10min.webp",
    "figure_04_c1_combined.png": "catchments-combined.webp",
    "figure_06_population_sensitivity_qa.png": "population-sensitivity.webp",
}

PDF_SOURCES = {
    "figure_00a_site_origin_selection.pdf": "origin-selection.pdf",
    "figure_02_c1_10min.pdf": "catchment-10min.pdf",
    "figure_04_c1_combined.pdf": "catchments-combined.pdf",
    "figure_06_population_sensitivity_qa.pdf": "population-sensitivity.pdf",
}


def export_webp(source: Path, destination: Path, max_width: int = 2000) -> None:
    with Image.open(source) as image:
        image = image.convert("RGB")
        if image.width > max_width:
            height = round(image.height * max_width / image.width)
            image = image.resize((max_width, height), Image.Resampling.LANCZOS)
        image.save(destination, "WEBP", quality=84, method=6)


def main() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    missing = [name for name in [*IMAGE_SOURCES, *PDF_SOURCES]
               if not (MAPS / name).exists()]
    if missing:
        raise FileNotFoundError(f"Generate map outputs first: {missing}")
    for source_name, destination_name in IMAGE_SOURCES.items():
        export_webp(MAPS / source_name, IMAGES / destination_name)
    for source_name, destination_name in PDF_SOURCES.items():
        shutil.copy2(MAPS / source_name, DOWNLOADS / destination_name)
    print(f"Built {len(IMAGE_SOURCES)} images and {len(PDF_SOURCES)} PDFs in {SITE}")


if __name__ == "__main__":
    main()
