"""Load and validate modelling settings before processing."""
from pathlib import Path
import math
import yaml
from pyproj import CRS
from .paths import ROOT


def load_config(path: Path | None = None) -> dict:
    """Return validated configuration, rejecting unsupported assumptions."""
    with (path or ROOT / "config.yaml").open(encoding="utf-8") as stream:
        cfg = yaml.safe_load(stream)
    if not isinstance(cfg, dict):
        raise ValueError("Configuration must be a mapping")
    try:
        crs = CRS.from_user_input(cfg["project"]["analysis_crs"])
        if crs.to_epsg() != 27700:
            raise ValueError("This workflow requires EPSG:27700")
        if CRS.from_user_input(cfg["project"]["geographic_crs"]).to_epsg() != 4326:
            raise ValueError("Geographic CRS must be EPSG:4326")
        for section, key in [("walking", "speed_kmh"), ("walking", "edge_buffer_m"),
                             ("osm", "download_buffer_m"),
                             ("site_origins", "contact_tolerance_m"),
                             ("site_origins", "min_access_separation_m")]:
            v = cfg[section][key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
                raise ValueError(f"{section}.{key} must be positive and finite")
        minutes = cfg["walking"]["sensitivity_minutes"]
        if not isinstance(minutes, list) or not minutes or any(type(m) is not int or m <= 0 for m in minutes):
            raise ValueError("Sensitivity minutes must be positive integers")
        if len(minutes) != len(set(minutes)) or cfg["walking"]["primary_catchment_minutes"] not in minutes:
            raise ValueError("Minutes must be unique and include the primary catchment")
        if cfg["osm"]["network_type"] != "walk":
            raise ValueError("OSM network_type must be walk")
        angle = cfg["site_origins"]["min_crossing_angle_degrees"]
        if type(angle) not in (int, float) or not math.isfinite(angle) or not 0 < angle <= 90:
            raise ValueError("site_origins.min_crossing_angle_degrees must be in (0, 90]")
        max_access_points = cfg["site_origins"]["max_access_points"]
        if type(max_access_points) is not int or max_access_points <= 0:
            raise ValueError("site_origins.max_access_points must be a positive integer")
        for section, key in [("osm", "use_cached_network"), ("population", "use_area_weighting"),
                             ("schools", "open_only"),
                             ("accessibility_extension", "enabled")]:
            if type(cfg[section][key]) is not bool:
                raise ValueError(f"{section}.{key} must be boolean")
        for key in ["london_bounds_bng", "bng_bounds"]:
            bounds = cfg["qa"][key]
            if len(bounds) != 4 or not all(math.isfinite(v) for v in bounds) or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
                raise ValueError(f"Invalid qa.{key}")
        for key in ["overlap_tolerance_m2", "site_area_relative_tolerance"]:
            if not math.isfinite(cfg["qa"][key]) or cfg["qa"][key] < 0:
                raise ValueError(f"Invalid qa.{key}")
        for key in ['distance_outlier_m', 'max_statutory_age']:
            if type(cfg['qa'][key]) not in (int, float) or not math.isfinite(cfg['qa'][key]) or cfg['qa'][key] <= 0:
                raise ValueError(f'qa.{key} must be positive and finite')
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Missing or malformed configuration: {exc}") from exc
    return cfg
