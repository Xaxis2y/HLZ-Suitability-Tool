# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Profile loading and strict validation.

Profiles are JSON files (standard library only, so they load inside the
default ArcGIS Pro environment). YAML files are still accepted when PyYAML is
importable, which keeps v0.3 profiles usable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import AircraftProfile, DoctrineProfile, MissionProfile

PROFILE_SUFFIXES = (".json", ".yaml", ".yml")


class ProfileError(ValueError):
    """Raised when a profile is missing, malformed, or unsafe."""


def project_root() -> Path:
    """Return the release root folder (parent of ``hlzcore``)."""
    return Path(__file__).resolve().parent.parent


def profiles_root() -> Path:
    return project_root() / "profiles"


def _read_profile(path: str | Path) -> dict[str, Any]:
    profile_path = Path(path)
    if not profile_path.is_file():
        raise ProfileError(f"Profile does not exist: {profile_path}")
    suffix = profile_path.suffix.lower()
    text = profile_path.read_text(encoding="utf-8")
    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise ProfileError(f"Invalid JSON in {profile_path}: {error}") from error
    elif suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as error:
            raise ProfileError(
                "YAML profiles need PyYAML, which is not installed in this "
                "environment. Use the JSON profile format instead."
            ) from error
        data = yaml.safe_load(text)
    else:
        raise ProfileError(f"Unsupported profile type {suffix!r}: {profile_path}")
    if not isinstance(data, dict):
        raise ProfileError(f"Profile root must be an object/mapping: {profile_path}")
    return {key: value for key, value in data.items() if not str(key).startswith("_")}


def _require(data: dict[str, Any], keys: set[str], label: str) -> None:
    missing = sorted(keys.difference(data))
    if missing:
        raise ProfileError(f"{label} profile is missing: {', '.join(missing)}")


def _number(name: str, value: Any, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ProfileError(f"{name} must be a number; received {value!r}") from error
    if number != number:  # NaN
        raise ProfileError(f"{name} must not be NaN")
    if number < 0.0 or (not allow_zero and number == 0.0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ProfileError(f"{name} must be {qualifier}; received {number}")
    return number


def _fraction(name: str, value: Any) -> float:
    number = _number(name, value)
    if number > 1.0:
        raise ProfileError(f"{name} must be within (0, 1]; received {number}")
    return number


def _optional_number(data: dict[str, Any], name: str) -> float | None:
    value = data.get(name)
    if value is None:
        return None
    return _number(name, value)


def load_aircraft_profile(path: str | Path) -> AircraftProfile:
    data = _read_profile(path)
    required = {
        "profile_id",
        "display_name",
        "tdp_diameter_m",
        "cleared_ring_m",
        "max_landing_slope_deg",
        "max_upslope_landing_deg",
        "max_roughness_m",
        "tdp_obstacle_height_m",
        "ring_obstacle_height_m",
        "approach_range_m",
        "corridor_half_width_deg",
        "suitable_angle_deg",
        "unsuitable_angle_deg",
        "clearance_height_m",
    }
    _require(data, required, "Aircraft")
    profile = AircraftProfile(
        profile_id=str(data["profile_id"]),
        display_name=str(data["display_name"]),
        tdp_diameter_m=_number("tdp_diameter_m", data["tdp_diameter_m"]),
        cleared_ring_m=_number("cleared_ring_m", data["cleared_ring_m"], True),
        max_landing_slope_deg=_number("max_landing_slope_deg", data["max_landing_slope_deg"]),
        max_upslope_landing_deg=_number(
            "max_upslope_landing_deg", data["max_upslope_landing_deg"]
        ),
        max_roughness_m=_number("max_roughness_m", data["max_roughness_m"]),
        tdp_obstacle_height_m=_number(
            "tdp_obstacle_height_m", data["tdp_obstacle_height_m"], True
        ),
        ring_obstacle_height_m=_number(
            "ring_obstacle_height_m", data["ring_obstacle_height_m"], True
        ),
        approach_range_m=_number("approach_range_m", data["approach_range_m"]),
        corridor_half_width_deg=_number(
            "corridor_half_width_deg", data["corridor_half_width_deg"]
        ),
        suitable_angle_deg=_number("suitable_angle_deg", data["suitable_angle_deg"], True),
        unsuitable_angle_deg=_number("unsuitable_angle_deg", data["unsuitable_angle_deg"]),
        clearance_height_m=_number("clearance_height_m", data["clearance_height_m"], True),
        hover_ceiling_ige_ft=_optional_number(data, "hover_ceiling_ige_ft"),
        hover_ceiling_oge_ft=_optional_number(data, "hover_ceiling_oge_ft"),
        description=str(data.get("description", "")),
    )
    if profile.max_upslope_landing_deg < profile.max_landing_slope_deg:
        raise ProfileError("max_upslope_landing_deg cannot be below max_landing_slope_deg")
    if profile.max_upslope_landing_deg >= 45.0:
        raise ProfileError("max_upslope_landing_deg must be below 45 degrees")
    if profile.unsuitable_angle_deg <= profile.suitable_angle_deg:
        raise ProfileError("unsuitable_angle_deg must be greater than suitable_angle_deg")
    if profile.unsuitable_angle_deg >= 45.0:
        raise ProfileError("unsuitable_angle_deg must be below 45 degrees")
    if profile.corridor_half_width_deg >= 90.0:
        raise ProfileError("corridor_half_width_deg must be below 90 degrees")
    if profile.ring_obstacle_height_m < profile.tdp_obstacle_height_m:
        raise ProfileError(
            "ring_obstacle_height_m cannot be below tdp_obstacle_height_m "
            "(the cleared ring may never be stricter than the touchdown point)"
        )
    if profile.approach_range_m <= profile.analysis_radius_m:
        raise ProfileError("approach_range_m must exceed the TDP radius plus cleared ring")
    return profile


def load_mission_profile(path: str | Path) -> MissionProfile:
    data = _read_profile(path)
    required = {
        "profile_id",
        "display_name",
        "slope_weight",
        "roughness_weight",
        "approach_weight",
        "obstacle_weight",
        "data_quality_weight",
    }
    _require(data, required, "Mission")
    try:
        max_candidates = int(data.get("max_candidates", 1000))
        max_candidates_per_tile = int(data.get("max_candidates_per_tile", 100))
        max_input_cells = int(data.get("max_input_cells", 16_000_000))
        max_surface_cells = int(data.get("max_surface_cells", 150_000_000))
        tile_target_cells = int(data.get("tile_target_cells", 1_500_000))
    except (TypeError, ValueError) as error:
        raise ProfileError(f"Mission integer setting is invalid: {error}") from error
    profile = MissionProfile(
        profile_id=str(data["profile_id"]),
        display_name=str(data["display_name"]),
        slope_weight=_number("slope_weight", data["slope_weight"], True),
        roughness_weight=_number("roughness_weight", data["roughness_weight"], True),
        approach_weight=_number("approach_weight", data["approach_weight"], True),
        obstacle_weight=_number("obstacle_weight", data["obstacle_weight"], True),
        data_quality_weight=_number("data_quality_weight", data["data_quality_weight"], True),
        azimuth_step_deg=_number("azimuth_step_deg", data.get("azimuth_step_deg", 10.0)),
        ray_step_deg=_number("ray_step_deg", data.get("ray_step_deg", 5.0)),
        max_candidates=max_candidates,
        max_candidates_per_tile=max_candidates_per_tile,
        minimum_candidate_separation_factor=_number(
            "minimum_candidate_separation_factor",
            data.get("minimum_candidate_separation_factor", 1.0),
        ),
        max_input_cells=max_input_cells,
        max_surface_cells=max_surface_cells,
        minimum_valid_fraction=_fraction(
            "minimum_valid_fraction", data.get("minimum_valid_fraction", 0.95)
        ),
        minimum_ring_coverage_fraction=_fraction(
            "minimum_ring_coverage_fraction",
            data.get("minimum_ring_coverage_fraction", 0.90),
        ),
        minimum_approach_coverage_fraction=_fraction(
            "minimum_approach_coverage_fraction",
            data.get("minimum_approach_coverage_fraction", 0.80),
        ),
        upslope_alignment_tolerance_deg=_number(
            "upslope_alignment_tolerance_deg",
            data.get("upslope_alignment_tolerance_deg", 30.0),
        ),
        tile_target_cells=tile_target_cells,
    )
    if profile.weight_total <= 0.0:
        raise ProfileError("Mission profile must have at least one positive weight")
    if profile.max_candidates < 1 or profile.max_candidates > 100_000:
        raise ProfileError("max_candidates must be between 1 and 100,000")
    if profile.max_candidates_per_tile < 1 or profile.max_candidates_per_tile > 5000:
        raise ProfileError("max_candidates_per_tile must be between 1 and 5,000")
    if profile.max_input_cells < 250_000:
        raise ProfileError("max_input_cells (cells per tile) must be at least 250,000")
    if profile.max_input_cells > 200_000_000:
        raise ProfileError("max_input_cells (cells per tile) must not exceed 200,000,000")
    if profile.tile_target_cells < 10_000:
        raise ProfileError("tile_target_cells must be at least 10,000")
    if profile.azimuth_step_deg > 45.0 or profile.ray_step_deg > 15.0:
        raise ProfileError("Approach sampling is too coarse (azimuth <= 45, ray <= 15 degrees)")
    ray_count = 360.0 / profile.ray_step_deg
    if abs(ray_count - round(ray_count)) > 1.0e-6:
        raise ProfileError("ray_step_deg must divide 360 evenly (e.g. 1, 2, 2.5, 5, 10)")
    if profile.upslope_alignment_tolerance_deg >= 90.0:
        raise ProfileError("upslope_alignment_tolerance_deg must be below 90 degrees")
    return profile


def load_doctrine_profile(path: str | Path) -> DoctrineProfile:
    data = _read_profile(path)
    required = {
        "profile_id",
        "display_name",
        "source_title",
        "source_edition",
        "approval_status",
    }
    _require(data, required, "Doctrine")
    profile = DoctrineProfile(
        profile_id=str(data["profile_id"]),
        display_name=str(data["display_name"]),
        source_title=str(data["source_title"]),
        source_edition=str(data["source_edition"]),
        approval_status=str(data["approval_status"]),
        highly_suitable_score=float(data.get("highly_suitable_score", 80.0)),
        suitable_score=float(data.get("suitable_score", 60.0)),
        marginal_score=float(data.get("marginal_score", 40.0)),
    )
    thresholds = (
        profile.highly_suitable_score,
        profile.suitable_score,
        profile.marginal_score,
    )
    if not (100.0 >= thresholds[0] > thresholds[1] > thresholds[2] >= 0.0):
        raise ProfileError("Doctrine score thresholds must descend within 0 to 100")
    return profile


def check_profile_compatibility(aircraft: AircraftProfile, mission: MissionProfile) -> None:
    """Cross-profile checks that need both profiles."""
    if mission.ray_step_deg > aircraft.corridor_half_width_deg:
        raise ProfileError(
            "ray_step_deg must not exceed the aircraft corridor_half_width_deg; "
            "otherwise some sectors would contain no rays"
        )


GROUP_ORDER = {"": 0, "canada": 1, "united_states": 2, "nato_europe": 3}
GROUP_TITLES = {"canada": "Canada", "united_states": "United States", "nato_europe": "NATO Europe"}


def list_profiles(kind: str) -> dict[str, Path]:
    """Return {display name: path} for every profile of a kind.

    ``kind`` is one of ``aircraft``, ``missions`` or ``doctrine``. Broken
    profiles are skipped so a single bad file never breaks the toolbox UI.
    """
    folder = profiles_root() / kind
    loader = {
        "aircraft": load_aircraft_profile,
        "missions": load_mission_profile,
        "doctrine": load_doctrine_profile,
    }[kind]
    result: dict[str, Path] = {}
    if not folder.is_dir():
        return result
    entries = []
    for path in folder.rglob("*"):
        relative = path.relative_to(folder)
        if not path.is_file() or path.suffix.lower() not in PROFILE_SUFFIXES:
            continue
        if any(part.startswith("_") for part in relative.parts):
            continue  # _catalog and similar helper folders
        try:
            profile = loader(path)
        except ProfileError:
            continue
        group = relative.parts[0] if len(relative.parts) > 1 else ""
        prefix = f"{GROUP_TITLES.get(group, group.replace('_', ' ').title())}: " if group else ""
        size = ""
        if kind == "aircraft":  # v0.6.8: show the landing-point size so the dropdown doubles as a size guide
            size = f" - {profile.tdp_diameter_m:g} m landing point + {profile.cleared_ring_m:g} m ring"
        entries.append(((GROUP_ORDER.get(group, 9), group, profile.display_name.lower()),
                        f"{prefix}{profile.display_name}{size} [{path.stem}]", path))
    for _, label, path in sorted(entries, key=lambda item: item[0]):
        result[label] = path
    return result
