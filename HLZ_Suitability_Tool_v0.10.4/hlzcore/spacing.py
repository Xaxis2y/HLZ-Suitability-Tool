# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Distinct sites among the ranked candidates (v0.9.0).

The engine already keeps candidate centres at least one landing-point diameter apart (``minimum_candidate_separation_factor``),
so the circles never overlap. In an open field that still lets the top of the ranking fill up with neighbours of the same
field: ten "different" candidates that are really one site. Two tools address that:

* ``thin_by_spacing``: optional. Keep a candidate only if it is at least ``spacing_m`` from every better candidate kept so far.
* ``site_groups``: always on, informational. Candidates closer than ``link_m`` to each other (chains included) form one group;
  the run log and report say how many separate groups the best candidates fall into.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from .models import Candidate


def thin_by_spacing(ranked: Sequence[Candidate], spacing_m: float) -> tuple[list[Candidate], int]:
    """Greedy thinning in rank order. ``ranked`` must be sorted best first. Returns (kept, number dropped)."""
    if spacing_m <= 0.0 or len(ranked) < 2:
        return list(ranked), 0
    from scipy.spatial import cKDTree

    xy = np.array([[c.x, c.y] for c in ranked], dtype=np.float64)
    tree = cKDTree(xy)
    neighbours = tree.query_ball_point(xy, r=spacing_m - 1.0e-9)  # strictly closer than the spacing
    alive = np.ones(len(ranked), dtype=bool)
    kept: list[Candidate] = []
    for index, candidate in enumerate(ranked):
        if not alive[index]:
            continue
        kept.append(candidate)
        for other in neighbours[index]:
            if other > index:
                alive[other] = False
    return kept, len(ranked) - len(kept)


def site_groups(candidates: Sequence[Candidate], link_m: float) -> np.ndarray:
    """Group number (0-based, in order of first appearance) per candidate; chains of neighbours within ``link_m`` merge."""
    count = len(candidates)
    if count == 0:
        return np.zeros(0, dtype=np.int64)
    if count == 1 or link_m <= 0.0:
        return np.arange(count, dtype=np.int64)
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    from scipy.spatial import cKDTree

    xy = np.array([[c.x, c.y] for c in candidates], dtype=np.float64)
    pairs = cKDTree(xy).query_pairs(r=link_m, output_type="ndarray")
    if len(pairs) == 0:
        return np.arange(count, dtype=np.int64)
    graph = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(count, count))
    _, labels = connected_components(graph, directed=False)
    # Renumber in order of first appearance so group 0 holds the best candidate.
    order: dict[int, int] = {}
    return np.array([order.setdefault(int(label), len(order)) for label in labels], dtype=np.int64)


def spacing_summary(ranked: Sequence[Candidate], link_m: float, top_n: int = 10) -> dict[str, int | float]:
    """How crowded the ranking is: groups overall and among the best ``top_n``."""
    groups = site_groups(ranked, link_m)
    top = groups[:top_n]
    return {
        "link_m": round(float(link_m), 1),
        "candidates": len(ranked),
        "groups": int(groups.max()) + 1 if len(groups) else 0,
        "top_n": int(min(top_n, len(ranked))),
        "top_groups": int(len(set(top.tolist()))),
    }
