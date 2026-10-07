# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Explainable candidate scoring, rating and ranking (v0.6.7).

v0.3 labelled every candidate ``SCREENING_CANDIDATE`` whenever the doctrine
profile was not approved, so a map of results showed no differentiation at
all. v0.6.7 separates two ideas:

* ``rating``  - what the evidence says (Highly suitable / Suitable / Marginal
  / Unsuitable / Unassessed), always computed;
* ``status``  - whether the thresholds behind that rating are approved
  doctrine or planning defaults.

Safety caps keep the rating conservative: without a DSM (trees and buildings
unknown) no candidate can be rated better than Marginal.
"""

from __future__ import annotations

from .models import (
    RATING_HIGHLY_SUITABLE,
    RATING_MARGINAL,
    RATING_ORDER,
    RATING_SUITABLE,
    RATING_UNASSESSED,
    RATING_UNSUITABLE,
    AircraftProfile,
    Candidate,
    DoctrineProfile,
    MissionProfile,
)

STATUS_APPROVED = "Approved profile"
STATUS_PLANNING = "Planning only - profile not approved"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _cap(rating: str, cap: str) -> str:
    """Return the worse of two ratings."""
    return rating if RATING_ORDER.index(rating) >= RATING_ORDER.index(cap) else cap


def _rating_from_score(score: float, doctrine: DoctrineProfile) -> str:
    if score >= doctrine.highly_suitable_score:
        return RATING_HIGHLY_SUITABLE
    if score >= doctrine.suitable_score:
        return RATING_SUITABLE
    if score >= doctrine.marginal_score:
        return RATING_MARGINAL
    return RATING_UNSUITABLE


def score_candidate(
    candidate: Candidate,
    aircraft: AircraftProfile,
    mission: MissionProfile,
    doctrine: DoctrineProfile,
    cell_size_m: float,
    obstacle_evidence_available: bool,
    performance_mode: str,
) -> Candidate:
    """Score one candidate and set an evidence-aware rating."""

    notes: list[str] = []

    # --- Slope -----------------------------------------------------------
    if candidate.upslope_only:
        candidate.slope_component = 0.5 * _clamp(
            1.0 - candidate.slope_deg / aircraft.max_upslope_landing_deg
        )
        notes.append(
            f"Slope {candidate.slope_deg:.1f} deg exceeds {aircraft.max_landing_slope_deg:.0f} deg: "
            "upslope landing only"
        )
    else:
        candidate.slope_component = _clamp(1.0 - candidate.slope_deg / aircraft.max_landing_slope_deg)

    # --- Roughness ----------------------------------------------------------
    candidate.roughness_component = _clamp(1.0 - candidate.roughness_m / aircraft.max_roughness_m)

    # --- Approach -----------------------------------------------------------
    # 80 % from the best sector's obstacle angle, 20 % from approach breadth
    # (share of all sectors that are clear; 25 % clear or more = full credit).
    # Breadth matters operationally: more clear directions = more wind options,
    # and sectors with no data (e.g. toward the raster edge) earn no credit.
    if candidate.best_approach_angle_deg is None:
        angle_part = 0.0
    elif candidate.best_approach_angle_deg <= aircraft.suitable_angle_deg:
        angle_part = 1.0
    else:
        span = aircraft.unsuitable_angle_deg - aircraft.suitable_angle_deg
        angle_part = _clamp(1.0 - (candidate.best_approach_angle_deg - aircraft.suitable_angle_deg) / span)
    sector_total = len(candidate.sectors) or 1
    breadth_part = _clamp((candidate.clear_sector_count / sector_total) / 0.25)
    candidate.approach_component = 0.8 * angle_part + 0.2 * breadth_part if angle_part > 0 else 0.0
    if candidate.opposing_corridors:
        candidate.approach_component = min(1.0, candidate.approach_component + 0.1)

    # --- Obstacles ----------------------------------------------------------
    if obstacle_evidence_available:
        inner_ratio = (candidate.max_tdp_obstacle_m or 0.0) / max(aircraft.tdp_obstacle_height_m, 1.0e-6)
        ring_ratio = (candidate.max_ring_obstacle_m or 0.0) / max(aircraft.ring_obstacle_height_m, 1.0e-6)
        candidate.obstacle_component = _clamp(1.0 - max(inner_ratio, ring_ratio))
    else:
        candidate.obstacle_component = 0.0
        notes.append("No DSM: trees/buildings at the LZ not assessed")

    # --- Data quality -------------------------------------------------------
    if cell_size_m <= 1.0 and obstacle_evidence_available:
        candidate.data_quality_component = 1.0
        candidate.confidence = "HIGH"
    elif cell_size_m <= 2.5 and obstacle_evidence_available:
        candidate.data_quality_component = 0.65
        candidate.confidence = "MEDIUM"
    else:
        candidate.data_quality_component = 0.25
        candidate.confidence = "SCREENING"
        if cell_size_m > 2.5:
            notes.append(f"Coarse DEM ({cell_size_m:.1f} m cells)")

    weighted_sum = (
        candidate.slope_component * mission.slope_weight
        + candidate.roughness_component * mission.roughness_weight
        + candidate.approach_component * mission.approach_weight
        + candidate.obstacle_component * mission.obstacle_weight
        + candidate.data_quality_component * mission.data_quality_weight
    )
    candidate.score = round(100.0 * weighted_sum / mission.weight_total, 1)
    candidate.status = STATUS_APPROVED if doctrine.is_approved else STATUS_PLANNING

    # --- Plain-language limiting factor ------------------------------------
    factors = {
        "slope": (
            candidate.slope_component,
            f"Slope {candidate.slope_deg:.1f} deg (limit {aircraft.max_landing_slope_deg:.0f} deg)",
        ),
        "roughness": (
            candidate.roughness_component,
            f"Surface roughness {candidate.roughness_m:.2f} m (limit {aircraft.max_roughness_m:.2f} m)",
        ),
        "approach": (
            candidate.approach_component,
            "No usable approach found"
            if candidate.best_approach_angle_deg is None
            else (
                f"Best approach obstacle angle {candidate.best_approach_angle_deg:.1f} deg "
                f"(clear <= {aircraft.suitable_angle_deg:.0f} deg)"
            ),
        ),
    }
    if obstacle_evidence_available:
        worst_obstacle = max(candidate.max_tdp_obstacle_m or 0.0, candidate.max_ring_obstacle_m or 0.0)
        factors["obstacle"] = (
            candidate.obstacle_component,
            f"Obstacle up to {worst_obstacle:.2f} m in LZ/cleared ring",
        )
    limiting_key = min(factors, key=lambda key: (factors[key][0], key))
    candidate.limiting_factor = factors[limiting_key][1]
    if factors[limiting_key][0] >= 0.9:
        candidate.limiting_factor = "None - all criteria well within limits"

    # --- Rating with hard gates and safety caps -----------------------------
    rating = _rating_from_score(candidate.score, doctrine)
    ceiling = (
        aircraft.hover_ceiling_oge_ft
        if performance_mode.upper() == "OGE"
        else aircraft.hover_ceiling_ige_ft
    )
    gate: str | None = None
    if (
        ceiling is not None
        and candidate.density_altitude_ft is not None
        and candidate.density_altitude_ft > ceiling
    ):
        gate = (
            f"Density altitude {candidate.density_altitude_ft:,.0f} ft exceeds "
            f"{performance_mode.upper()} hover ceiling {ceiling:,.0f} ft"
        )
        rating = RATING_UNSUITABLE
    elif candidate.sectors and candidate.assessed_sector_count == 0:
        gate = "Approach not assessable (data does not cover the approach area)"
        rating = RATING_UNASSESSED
    elif candidate.sectors and candidate.best_approach_angle_deg is None:
        gate = (
            "Upslope landing required but no approach lines up with the upslope heading"
            if candidate.upslope_only
            else "No usable approach sector"
        )
        rating = RATING_UNSUITABLE
    elif (
        candidate.best_approach_angle_deg is not None
        and candidate.best_approach_angle_deg > aircraft.unsuitable_angle_deg
    ):
        gate = (
            f"All approaches blocked (best {candidate.best_approach_angle_deg:.1f} deg > "
            f"{aircraft.unsuitable_angle_deg:.0f} deg)"
        )
        rating = RATING_UNSUITABLE
    elif (
        candidate.best_approach_angle_deg is not None
        and candidate.best_approach_angle_deg > aircraft.suitable_angle_deg
    ):
        rating = _cap(rating, RATING_MARGINAL)
        notes.append("Steep approach only (no sector within the normal glide angle)")

    if gate is None:
        if candidate.upslope_only:
            rating = _cap(rating, RATING_SUITABLE)
        if not obstacle_evidence_available:
            capped = _cap(rating, RATING_MARGINAL)
            if capped != rating:
                candidate.limiting_factor = "Obstacles not assessed (no DSM) - rating capped at Marginal"
            rating = capped
        if candidate.approach_basis == "TERRAIN_ONLY":
            notes.append("Approach checked against bare terrain only")
    else:
        candidate.limiting_factor = gate

    if ceiling is None and candidate.density_altitude_ft is not None:
        notes.append("No hover ceiling in aircraft profile: density altitude not gated")

    candidate.rating = rating
    candidate.notes = "; ".join(dict.fromkeys(notes))
    return candidate


def rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Sort best-first, assign ranks and readable IDs (HLZ-01, HLZ-02, ...)."""
    from .tiling import rank_key

    ordered = sorted(candidates, key=rank_key)
    width = max(2, len(str(len(ordered))))
    for rank, candidate in enumerate(ordered, start=1):
        candidate.rank = rank
        candidate.candidate_id = f"HLZ-{rank:0{width}d}"
    return ordered


def rating_counts(candidates: list[Candidate]) -> dict[str, int]:
    counts = {rating: 0 for rating in RATING_ORDER}
    for candidate in candidates:
        counts[candidate.rating] = counts.get(candidate.rating, 0) + 1
    return counts
