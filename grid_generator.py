"""
grid_generator.py
-------------------
`generate_rectangular_grid` is the original PHASE 1 function: a plain
width x length rectangle, no shapely dependency. It's kept as-is and
still backs the phase-1 `/api/heatmap` route — nothing about it needed to
change.

`generate_measurement_grid` is the PHASE 2 addition: the same "target
spacing, inset from the walls" idea, but clipped to an arbitrary
`RoomShape` (rectangle, concave polygon, or polygon with holes) via
shapely, per plan section 5/6. Both return the same `GridPoint` list, so
`aggregator.py` and `heatmap_render.py` don't need to know which one
produced their input.
"""

from typing import List

from shapely.geometry import Point

from models import GridPoint
from room_geometry import RoomShape


def generate_rectangular_grid(
    width: float,
    length: float,
    spacing: float,
    wall_margin: float,
) -> List[GridPoint]:
    """Generate a regular grid of calculation points inside a
    width x length rectangle, inset by `wall_margin` on every side, with
    approximately `spacing` meters between points.

    Raises ValueError if the margin leaves no usable interior - that's a
    configuration mistake and should be reported clearly rather than
    silently returning an empty/degenerate grid.
    """
    usable_width = width - 2 * wall_margin
    usable_length = length - 2 * wall_margin

    if usable_width <= 0 or usable_length <= 0:
        raise ValueError(
            f"Wall margin ({wall_margin} m) leaves no usable interior for a "
            f"{width} x {length} m room."
        )

    cols = max(1, round(usable_width / spacing) + 1)
    rows = max(1, round(usable_length / spacing) + 1)

    # Recompute actual spacing from the rounded point count so points are
    # evenly spread across the usable interior rather than bunched at one
    # edge - this is the "report the ACTUAL value, not the target"
    # principle from the engineering plan, applied here too.
    x_step = usable_width / (cols - 1) if cols > 1 else 0.0
    y_step = usable_length / (rows - 1) if rows > 1 else 0.0

    points: List[GridPoint] = []
    for row in range(rows):
        y = wall_margin + (row * y_step if rows > 1 else usable_length / 2)
        for col in range(cols):
            x = wall_margin + (col * x_step if cols > 1 else usable_width / 2)
            points.append(GridPoint(x=x, y=y))

    return points


def generate_measurement_grid(
    shape: "RoomShape",
    resolution: float,
    wall_margin: float,
) -> List[GridPoint]:
    """Generate calculation points inside `shape`, inset by `wall_margin`,
    at approximately `resolution` meters spacing — clipped to the actual
    (possibly concave, possibly holed) polygon rather than its bounding
    box.

    Independent resolution/margin from fixture spacing on purpose:
    measurement density and fixture spacing are different concerns (see
    plan §5, grid_generator.py). Raises ValueError if the margin leaves
    no usable interior, or if clipping happens to remove every candidate
    point (e.g. a resolution much coarser than a thin room).
    """
    measurement_region = shape.eroded(wall_margin)
    if measurement_region.is_empty:
        raise ValueError(
            f"Wall margin ({wall_margin} m) leaves no usable interior for this shape."
        )

    minx, miny, maxx, maxy = measurement_region.bounds
    usable_width = maxx - minx
    usable_length = maxy - miny

    if usable_width <= 0 or usable_length <= 0:
        raise ValueError("Measurement region has zero extent after applying the wall margin.")

    cols = max(1, round(usable_width / resolution) + 1)
    rows = max(1, round(usable_length / resolution) + 1)

    # Same "recompute the actual step from the rounded point count"
    # principle as generate_rectangular_grid, applied over the eroded
    # region's bounding box before the polygon clip is applied.
    x_step = usable_width / (cols - 1) if cols > 1 else 0.0
    y_step = usable_length / (rows - 1) if rows > 1 else 0.0

    points: List[GridPoint] = []
    for row in range(rows):
        y = miny + (row * y_step if rows > 1 else usable_length / 2)
        for col in range(cols):
            x = minx + (col * x_step if cols > 1 else usable_width / 2)
            # .covers() not .contains(): points exactly on the eroded
            # region's edge (which the grid construction below produces
            # at its own min/max bounds) must count as inside - see
            # RoomShape.contains's docstring for why plain shapely
            # `contains` would silently drop them.
            if measurement_region.covers(Point(x, y)):
                points.append(GridPoint(x=x, y=y))

    if not points:
        raise ValueError(
            "No measurement points fall inside the shape after clipping - "
            "check wall margin and resolution against the room/shape size."
        )

    return points
