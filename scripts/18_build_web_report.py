"""Build lightweight, commit-ready assets for the static GitHub Pages report."""
from __future__ import annotations

from pathlib import Path
import shutil
import argparse
import sys
import hashlib
import re

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MAPS = ROOT / "outputs" / "maps"
SITE = ROOT / "docs"
IMAGES = SITE / "assets" / "images"
DOWNLOADS = SITE / "assets" / "downloads"

IMAGE_SOURCES = {
    "figure_00a_site_origin_selection.png": "origin-selection.webp",
    "figure_00b_all_cleaned_schools.png": "schools-overview.webp",
    "figure_02_c1_10min.png": "catchment-10min.webp",
    "figure_01_c1_05min.png": "catchment-5min.webp",
    "figure_03_c1_15min.png": "catchment-15min.webp",
    "figure_04_c1_combined.png": "catchments-combined.webp",
    "figure_06_population_sensitivity_qa.png": "population-sensitivity.webp",
    "figure_07_schools_within_10min.png": "schools-within-10min.webp",
}

PDF_SOURCES = {
    "figure_00b_all_cleaned_schools.pdf": "schools-overview.pdf",
    "figure_00a_site_origin_selection.pdf": "origin-selection.pdf",
    "figure_02_c1_10min.pdf": "catchment-10min.pdf",
    "figure_04_c1_combined.pdf": "catchments-combined.pdf",
    "figure_06_population_sensitivity_qa.pdf": "population-sensitivity.pdf",
    "figure_07_schools_within_10min.pdf": "schools-within-10min.pdf",
    "figure_01_c1_05min.pdf": "catchment-5min.pdf",
    "figure_03_c1_15min.pdf": "catchment-15min.pdf",
    "figure_05_c1_combined_2400m_diameter.pdf": "straight-line-reference.pdf",
}


def export_webp(source: Path, destination: Path, max_width: int = 2000) -> None:
    with Image.open(source) as image:
        image = image.convert("RGB")
        if image.width > max_width:
            height = round(image.height * max_width / image.width)
            image = image.resize((max_width, height), Image.Resampling.LANCZOS)
        image.save(destination, "WEBP", quality=84, method=6)


def version_asset_links() -> None:
    """Change asset URLs when their contents change, avoiding stale browser caches."""
    page = SITE / 'index.html'
    def version(match):
        attribute, url = match.groups()
        relative = url.split('?', 1)[0]
        if not relative.startswith('assets/') and relative != 'favicon.svg':
            return match.group(0)
        asset = SITE / relative
        if not asset.is_file():
            raise FileNotFoundError(f'Report asset is missing: {relative}')
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()[:12]
        return f'{attribute}="{relative}?v={digest}"'
    page.write_text(re.sub(r'\b(src|href)="([^"]+)"', version,
                           page.read_text(encoding='utf-8')), encoding='utf-8')


def main() -> None:
    from src.final_delivery import build_report, render_delivery_maps
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--render-maps', action='store_true',
                        help='Redraw saved final geometries without rerunning analysis')
    args = parser.parse_args()
    selected, schools, _, summary, _ = build_report()
    if args.render_maps or not (MAPS / 'figure_07_schools_within_10min.png').exists():
        render_delivery_maps(selected, schools, summary)
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
    version_asset_links()
    print(f"Built {len(IMAGE_SOURCES)} images and {len(PDF_SOURCES)} PDFs in {SITE}")


if __name__ == "__main__":
    main()
