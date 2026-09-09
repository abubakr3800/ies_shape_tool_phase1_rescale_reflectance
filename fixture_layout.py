"""
fixture_layout.py
------------------
PHASE 2. Duplicates a fixture on a grid/staggered lattice with configurable
spacing and rotation, clipped to an arbitrary `RoomShape` — concave rooms
and rooms with holes (obstacles) included, since the clipping is just
`placement_region.contains(point)` on a shapely geometry that already
handles both correctly.

Per the engineering plan (§5, fixture_layout.py): never report the input
*target* spacing as if it were the *achieved* one — this was the single
biggest bug in the earlier LuxScale uniformity work. The achieved spacing
here is recomputed from each retained point's nearest neighbor, so a
clipped/irregular lattice reports what actually resulted, not what was
asked for.
"""

import math
from dataclasses import dataclass
from typing import List, Tuple

from shapely.geometry import Point

from room_geometry import RoomShape

Coordinate = Tuple[float, float]


@dataclass
class LayoutResult:
    positions: List[Coordinate]
    actual_spacing_x: float
    actual_spacing_y: float
    requested_spacing_x: float
    requested_spacing_y: float
    pattern: str
    rotation_deg: float
    wall_margin: float

    @property
    def count(self) -> int:
        return len(self.positions)


def generate_lattice(
    shape: RoomShape,
    spacing_x: float,
    spacing_y: float,
    pattern: str = "grid",
    rotation_deg: float = 0.0,
    wall_margin: float = 0.0,
) -> LayoutResult:
    """Generate fixture positions on a lattice, clipped to `shape`.

    Steps (mirrors the plan's §5 fixture_layout.py breakdown):
      1. Erode `shape` by `wall_margin` -> placement region.
      2. Build a lattice over the placement region's bounding box,
         oversized so rotation doesn't leave gaps at the corners,
         optionally offset every other row for a staggered/hex pattern.
      3. Filter every lattice point through `placement_region.contains` -
         shapely handles concavity and holes with no special-casing here.
      4. Recompute the actual achieved spacing from the retained points.

    Raises ValueError for non-positive spacing, an unknown pattern, a
    margin that erodes the shape to nothing, or a spacing/rotation
    combination that happens to leave zero points inside the region.
    """
    if spacing_x <= 0 or spacing_y <= 0:
        raise ValueError(
            f"spacing_x and spacing_y must both be positive (got {spacing_x}, {spacing_y})."
        )
    if pattern not in ("grid", "staggered"):
        raise ValueError(f"Unknown pattern '{pattern}' (expected 'grid' or 'staggered').")

    placement_region = shape.eroded(wall_margin)
    if placement_region.is_empty:
        raise ValueError(
            f"Wall margin ({wall_margin} m) leaves no usable placement area inside this shape."
        )

    minx, miny, maxx, maxy = placement_region.bounds
    center_x, center_y = (minx + maxx) / 2.0, (miny + maxy) / 2.0

    # Oversize the candidate lattice so a rotated pattern still fully
    # covers the placement region's bounding box out to its corners.
    diagonal = math.hypot(maxx - minx, maxy - miny)
    half_span = diagonal / 2.0 + max(spacing_x, spacing_y)

    theta = math.radians(rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    steps_x = int(math.ceil(half_span / spacing_x)) + 1
    steps_y = int(math.ceil(half_span / spacing_y)) + 1

    # Keep each retained point's lattice (i, j) index alongside its world
    # coordinates - the index pair is what lets _actual_spacing recompute
    # achieved spacing unambiguously (see its docstring for why raw
    # nearest-neighbor distance is not good enough on a square lattice).
    retained_indexed: List[Tuple[int, int, float, float]] = []
    for j in range(-steps_y, steps_y + 1):
        row_offset = (spacing_x / 2.0) if (pattern == "staggered" and j % 2 != 0) else 0.0
        local_y = j * spacing_y
        for i in range(-steps_x, steps_x + 1):
            local_x = i * spacing_x + row_offset
            # Rotate the lattice-local point into world space, then
            # translate to the placement region's bounding-box center.
            world_x = center_x + local_x * cos_t - local_y * sin_t
            world_y = center_y + local_x * sin_t + local_y * cos_t
            # .covers() not .contains(): points exactly on the eroded
            # region's edge must count as inside - see
            # RoomShape.contains's docstring.
            if placement_region.covers(Point(world_x, world_y)):
                retained_indexed.append((i, j, world_x, world_y))

    if not retained_indexed:
        raise ValueError(
            "No fixture positions fall inside the shape for this spacing/rotation - "
            "check spacing, wall margin, and rotation against the room size."
        )

    actual_x, actual_y = _actual_spacing(retained_indexed, spacing_x, spacing_y)
    retained = [(x, y) for (_i, _j, x, y) in retained_indexed]

    return LayoutResult(
        positions=retained,
        actual_spacing_x=actual_x,
        actual_spacing_y=actual_y,
        requested_spacing_x=spacing_x,
        requested_spacing_y=spacing_y,
        pattern=pattern,
        rotation_deg=rotation_deg,
        wall_margin=wall_margin,
    )


def _actual_spacing(
    retained_indexed: List[Tuple[int, int, float, float]],
    spacing_x: float,
    spacing_y: float,
) -> Tuple[float, float]:
    """Recompute achieved spacing from the retained points' own lattice
    indices, rather than a raw nearest-neighbor search.

    Why not nearest-neighbor distance: on a plain square lattice
    (spacing_x == spacing_y, pattern="grid"), every interior point has
    FOUR equally-near neighbors (up/down/left/right) - "the" nearest
    neighbor is a tie, so whichever one a distance search happens to pick
    first is arbitrary, and naively splitting that single vector into
    x/y components produces nonsense (e.g. reporting an x-spacing of 0
    because the arbitrarily-chosen nearest neighbor happened to be
    directly above, not beside). Grouping by the lattice's own (i, j)
    generation indices sidesteps the ambiguity entirely and also
    correctly captures the plan's actual intent: where a point's would-be
    neighbor got clipped out by a concave shape or hole, the gap to the
    next surviving point in that row/column is larger than the target -
    that enlarged gap is exactly what should get reported.

    x-spacing: within each row (fixed j), the gap between consecutive
    surviving i's, in units of `spacing_x` (row_offset for staggered
    patterns is constant within a row, so it cancels out of the
    difference). y-spacing: within each column (fixed i), the gap between
    consecutive surviving j's, in units of `spacing_y` (row offset only
    shifts local_x, never local_y, so this holds for "grid" and
    "staggered" alike). Reports the median gap across all rows/columns.
    """
    by_row: dict = {}
    by_col: dict = {}
    for i, j, _x, _y in retained_indexed:
        by_row.setdefault(j, []).append(i)
        by_col.setdefault(i, []).append(j)

    x_gaps: List[float] = []
    for i_values in by_row.values():
        i_sorted = sorted(set(i_values))
        for a, b in zip(i_sorted, i_sorted[1:]):
            x_gaps.append((b - a) * spacing_x)

    y_gaps: List[float] = []
    for j_values in by_col.values():
        j_sorted = sorted(set(j_values))
        for a, b in zip(j_sorted, j_sorted[1:]):
            y_gaps.append((b - a) * spacing_y)

    actual_x = _median(x_gaps) if x_gaps else 0.0
    actual_y = _median(y_gaps) if y_gaps else 0.0
    return actual_x, actual_y


def _median(values: List[float]) -> float:
    values = sorted(values)
    n = len(values)
    mid = n // 2
    if n % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0
