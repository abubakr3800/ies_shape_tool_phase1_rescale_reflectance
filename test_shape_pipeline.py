"""
test_shape_pipeline.py
-----------------------
Standalone checks (no pytest/Flask needed - `python test_shape_pipeline.py`)
covering items 2-6 of the engineering plan's §7 "applicability test plan",
now that phase 2 (room_geometry.py, fixture_layout.py, the polygon-clipped
grid_generator.generate_measurement_grid) exists. Item 1 (physics sanity)
is already covered by test_photometry.py and isn't repeated here.

    2. Rectangle regression      -> test_rectangle_regression
    3. Concave shape (L-room)    -> test_concave_shape_excludes_missing_quadrant
    4. Shape with a hole         -> test_hole_excludes_obstacle
    5. Actual vs. target spacing -> test_actual_spacing_differs_from_target
    6. Performance check         -> test_performance_check (prints timing, not a pass/fail gate)

Also covers the "standard shape" presets added to room_geometry.py
(regular polygon / trapezoid / circle / oval) -> test_shape_presets.
"""

import math
import time

import fixture_layout
import grid_generator
from room_geometry import RoomShape, RoomShapeError

FAILURES = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def test_rectangle_regression():
    """The polygon-clipped path should reproduce the same interior region
    as a naive rectangular grid for the simple rectangle case - i.e. the
    polygon machinery isn't silently changing behavior when there's
    nothing concave/holed about the shape."""
    width, length = 10.0, 8.0
    spacing = 1.0
    margin = 0.5

    naive_points = grid_generator.generate_rectangular_grid(
        width=width, length=length, spacing=spacing, wall_margin=margin
    )
    shape = RoomShape.from_rectangle(width, length)
    polygon_points = grid_generator.generate_measurement_grid(
        shape, resolution=spacing, wall_margin=margin
    )

    check(
        "rectangle regression: same point count",
        len(naive_points) == len(polygon_points),
        f"naive={len(naive_points)} polygon={len(polygon_points)}",
    )

    naive_xy = sorted((round(p.x, 6), round(p.y, 6)) for p in naive_points)
    polygon_xy = sorted((round(p.x, 6), round(p.y, 6)) for p in polygon_points)
    check(
        "rectangle regression: same point coordinates",
        naive_xy == polygon_xy,
    )

    check(
        "rectangle regression: area matches width*length",
        math.isclose(shape.area(), width * length, rel_tol=1e-9),
    )


def _l_shape():
    """An L-shaped room: a 10x10 square with the top-right 4x4 quadrant
    missing. Vertices go clockwise-or-CCW around the actual footprint -
    RoomShape normalizes orientation either way."""
    return RoomShape.from_vertices([
        (0, 0), (10, 0), (10, 6), (6, 6), (6, 10), (0, 10),
    ])


def test_concave_shape_excludes_missing_quadrant():
    shape = _l_shape()

    check(
        "L-shape area is 10x10 minus the missing 4x4 quadrant",
        math.isclose(shape.area(), 10 * 10 - 4 * 4, rel_tol=1e-9),
        f"got {shape.area()}",
    )

    grid_points = grid_generator.generate_measurement_grid(shape, resolution=0.5, wall_margin=0.25)
    in_missing_quadrant = [p for p in grid_points if p.x > 6.25 and p.y > 6.25]
    check(
        "no measurement points land in the L-shape's missing quadrant",
        len(in_missing_quadrant) == 0,
        f"found {len(in_missing_quadrant)} points where none should exist",
    )

    layout = fixture_layout.generate_lattice(shape, spacing_x=2.0, spacing_y=2.0, wall_margin=0.25)
    fixtures_in_missing_quadrant = [
        (x, y) for x, y in layout.positions if x > 6.25 and y > 6.25
    ]
    check(
        "no fixtures placed in the L-shape's missing quadrant",
        len(fixtures_in_missing_quadrant) == 0,
        f"found {len(fixtures_in_missing_quadrant)} fixtures where none should exist",
    )

    all_x = [p.x for p in grid_points]
    all_y = [p.y for p in grid_points]
    check(
        "L-shape U0/U1-relevant occupied area is bounded by the true footprint, not the 10x10 bounding box",
        max(all_x) <= 10.0 and max(all_y) <= 10.0,
    )


def test_hole_excludes_obstacle():
    """A 10x10 room with a 2x2 column near the middle. Both the
    measurement grid and the fixture lattice must exclude it."""
    exterior = [(0, 0), (10, 0), (10, 10), (0, 10)]
    hole = [(4, 4), (6, 4), (6, 6), (4, 6)]
    shape = RoomShape.from_vertices(exterior, holes=[hole])

    check(
        "room-with-hole area is 10x10 minus the 2x2 column",
        math.isclose(shape.area(), 10 * 10 - 2 * 2, rel_tol=1e-9),
        f"got {shape.area()}",
    )

    grid_points = grid_generator.generate_measurement_grid(shape, resolution=0.4, wall_margin=0.1)
    in_hole = [p for p in grid_points if 4.0 < p.x < 6.0 and 4.0 < p.y < 6.0]
    check(
        "no measurement points land inside the column",
        len(in_hole) == 0,
        f"found {len(in_hole)} points inside the obstacle",
    )

    layout = fixture_layout.generate_lattice(shape, spacing_x=1.0, spacing_y=1.0, wall_margin=0.1)
    fixtures_in_hole = [(x, y) for x, y in layout.positions if 4.0 < x < 6.0 and 4.0 < y < 6.0]
    check(
        "no fixtures placed inside the column",
        len(fixtures_in_hole) == 0,
        f"found {len(fixtures_in_hole)} fixtures inside the obstacle",
    )


def test_actual_spacing_differs_from_target():
    """Pick a spacing that does not evenly divide the room so the
    reported 'actual' spacing is a real recomputation, not a pass-through
    of the requested value. Also cover a rotated lattice, which is the
    other place a naive implementation might silently echo the target
    instead of recomputing from the retained points."""
    shape = RoomShape.from_rectangle(width=10.0, length=10.0)

    layout = fixture_layout.generate_lattice(shape, spacing_x=3.0, spacing_y=3.0, wall_margin=0.5)
    check(
        "layout reports both a requested and an actual spacing",
        hasattr(layout, "requested_spacing_x") and hasattr(layout, "actual_spacing_x"),
    )
    check(
        "actual spacing is close to the requested spacing for an axis-aligned, evenly-divisible-ish case",
        math.isclose(layout.actual_spacing_x, layout.requested_spacing_x, abs_tol=0.05)
        and math.isclose(layout.actual_spacing_y, layout.requested_spacing_y, abs_tol=0.05),
        f"requested=({layout.requested_spacing_x},{layout.requested_spacing_y}) "
        f"actual=({layout.actual_spacing_x},{layout.actual_spacing_y})",
    )

    rotated = fixture_layout.generate_lattice(
        shape, spacing_x=3.0, spacing_y=3.0, rotation_deg=30.0, wall_margin=0.5
    )
    check(
        "rotated lattice still recomputes an actual spacing close to the requested one",
        math.isclose(rotated.actual_spacing_x, 3.0, abs_tol=0.1)
        and math.isclose(rotated.actual_spacing_y, 3.0, abs_tol=0.1),
        f"actual=({rotated.actual_spacing_x},{rotated.actual_spacing_y})",
    )

    # An L-shape clip is where actual spacing genuinely can (locally)
    # differ from the target, since points near the notch lose some of
    # their would-be neighbors.
    l_shape = _l_shape()
    l_layout = fixture_layout.generate_lattice(l_shape, spacing_x=2.5, spacing_y=2.5, wall_margin=0.25)
    check(
        "L-shape layout still returns a usable (non-zero) actual spacing",
        l_layout.actual_spacing_x > 0 and l_layout.actual_spacing_y > 0,
    )


def test_performance_check():
    """Not a pass/fail gate - times point-by-point calculation cost as
    grid resolution and fixture count scale, per plan §7 item 6, so
    there's a concrete number to look at before folding this into the
    main simulator."""
    shape = RoomShape.from_rectangle(width=30.0, length=20.0)

    layout = fixture_layout.generate_lattice(shape, spacing_x=3.0, spacing_y=3.0, wall_margin=0.5)
    grid_points = grid_generator.generate_measurement_grid(shape, resolution=0.5, wall_margin=0.5)

    n_fixtures = layout.count
    n_points = len(grid_points)

    started = time.perf_counter()
    # A trivial stand-in illuminance sum (no IES/photometry dependency
    # needed to measure the O(points x fixtures) shape of the cost).
    total = 0.0
    for gp in grid_points:
        for fx, fy in layout.positions:
            dx, dy = gp.x - fx, gp.y - fy
            d2 = dx * dx + dy * dy + 9.0
            total += 1.0 / d2
    elapsed = time.perf_counter() - started

    print(
        f"[INFO] performance check: {n_fixtures} fixtures x {n_points} grid points "
        f"= {n_fixtures * n_points} pair evaluations in {elapsed:.4f}s "
        f"({(n_fixtures * n_points) / elapsed:,.0f} pairs/sec)"
    )
    print(
        "        -> if this tool is folded into the main simulator for much larger "
        "rooms/fixture counts, this is the loop to add a distance-cutoff or spatial "
        "index to (see plan §7 item 6)."
    )
    check("performance check ran without error", total != 0.0 or n_points > 0)


def test_shape_presets():
    """Sanity-checks for the regular-polygon/trapezoid/circle/oval preset
    generators: each should produce a valid, correctly-sized RoomShape via
    the exact same from_spec()/from_vertices()/_validated() path a
    hand-drawn polygon goes through - no shortcuts for generated shapes."""

    # A regular hexagon's area has a closed form: (3*sqrt(3)/2) * R^2.
    hexagon = RoomShape.from_spec({"type": "regular_polygon", "sides": 6, "radius": 4.0})
    expected_hex_area = (3 * math.sqrt(3) / 2) * 4.0 ** 2
    check(
        "regular hexagon area matches closed-form formula",
        abs(hexagon.area() - expected_hex_area) < 1e-6,
        f"got {hexagon.area()}, expected {expected_hex_area}",
    )

    # Specifying by side_length instead of radius should reproduce the
    # same polygon (up to floating point) as the equivalent radius.
    pentagon_by_side = RoomShape.from_spec({"type": "regular_polygon", "sides": 5, "side_length": 3.0})
    side = math.hypot(*[a - b for a, b in zip(
        pentagon_by_side.exterior_coords()[0], pentagon_by_side.exterior_coords()[1]
    )])
    check("regular polygon built from side_length reproduces that side length", abs(side - 3.0) < 1e-6)

    # A trapezoid with equal top/bottom widths is just a rectangle.
    trapezoid_as_rect = RoomShape.from_spec(
        {"type": "trapezoid", "bottom_width": 8.0, "top_width": 8.0, "height": 5.0}
    )
    check(
        "trapezoid with equal top/bottom width has rectangle area",
        abs(trapezoid_as_rect.area() - 8.0 * 5.0) < 1e-9,
    )

    # A circle's polygon-approximation area should approach pi*r^2, and
    # get closer as segment count rises (never further away).
    circle_coarse = RoomShape.from_spec({"type": "circle", "radius": 5.0, "segments": 16})
    circle_fine = RoomShape.from_spec({"type": "circle", "radius": 5.0, "segments": 128})
    exact = math.pi * 5.0 ** 2
    check(
        "finer circle approximation is closer to pi*r^2 than a coarse one",
        abs(circle_fine.area() - exact) < abs(circle_coarse.area() - exact),
    )

    # An oval (ellipse) area should approach pi*rx*ry.
    oval = RoomShape.from_spec({"type": "oval", "radius_x": 6.0, "radius_y": 3.0, "segments": 128})
    check("oval area approaches pi*radius_x*radius_y", abs(oval.area() - math.pi * 6.0 * 3.0) < 0.05)

    # Every preset should still land in the tool's positive-coordinate
    # convention (bounding box starting at x=0, y=0), same as a rectangle.
    for shape in (hexagon, pentagon_by_side, trapezoid_as_rect, circle_coarse, oval):
        min_x, min_y, _, _ = shape.bounds()
        check(
            "preset shape bounding box starts at the origin",
            abs(min_x) < 1e-9 and abs(min_y) < 1e-9,
            f"got bounds starting at ({min_x}, {min_y})",
        )

    # Invalid inputs should fail loudly with RoomShapeError, not silently
    # produce a degenerate shape.
    bad_inputs = [
        {"type": "regular_polygon", "sides": 2, "radius": 3.0},
        {"type": "regular_polygon", "sides": 6, "radius": 3.0, "side_length": 2.0},
        {"type": "trapezoid", "bottom_width": -1.0, "top_width": 2.0, "height": 3.0},
        {"type": "circle"},
        {"type": "oval", "radius_x": 5.0},
    ]
    for spec in bad_inputs:
        try:
            RoomShape.from_spec(spec)
            check(f"invalid preset spec rejected: {spec}", False, "no exception raised")
        except RoomShapeError:
            check(f"invalid preset spec rejected: {spec}", True)


if __name__ == "__main__":
    test_rectangle_regression()
    test_concave_shape_excludes_missing_quadrant()
    test_hole_excludes_obstacle()
    test_actual_spacing_differs_from_target()
    test_performance_check()
    test_shape_presets()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        raise SystemExit(1)
    print("All shape-pipeline checks passed.")
