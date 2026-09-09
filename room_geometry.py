"""
room_geometry.py
----------------
PHASE 2. Wraps a `shapely` polygon so the rest of the tool never touches
shapely directly — every other module just calls `RoomShape.contains(...)`,
`.eroded(...)`, `.area()`, etc. This is the module the engineering plan
flags as the one most likely to go wrong (concave rooms, rooms with
obstacles), so it leans entirely on shapely's well-tested polygon ops
instead of hand-rolled point-in-polygon/erosion code.

Two shape sources are supported, matching the plan's open question on
input method (§9): a plain rectangle (`from_rectangle`), and an arbitrary
polygon given as a vertex list with optional hole vertex lists
(`from_vertices`) — the "vertex-list/JSON" option from that question,
which is what the phase-2 frontend uses for anything non-rectangular.

On top of those two primitives, a handful of named "preset" generators
(`from_regular_polygon`, `from_trapezoid`, `from_ellipse`) build the
vertex list for a common room outline from a few dimension inputs
(number of sides + radius, top/bottom width + height, etc.) instead of
making the user type vertices by hand. Every preset still ends up going
through `from_vertices`/`_validated`, so a preset room is validated
exactly the same way as a hand-drawn polygon — there is no separate
"trusted" code path for generated shapes.
"""

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.validation import explain_validity, make_valid

Coordinate = Tuple[float, float]


class RoomShapeError(ValueError):
    """Raised when a shape definition doesn't describe a valid, usable
    simple polygon — bad vertex lists should fail loudly here rather than
    producing a silently-wrong area or point-in-polygon result downstream."""


def _largest_polygon(geom: BaseGeometry) -> Optional[Polygon]:
    """`shapely.make_valid` can turn a badly self-intersecting input into
    a MultiPolygon or GeometryCollection (e.g. it splits at a bowtie
    self-intersection). Take the largest-area Polygon piece rather than
    silently picking whichever comes first — this is a best-effort repair,
    not a substitute for the caller fixing their vertex list, so we still
    only return a single polygon for the rest of the tool to use."""
    if isinstance(geom, Polygon):
        return geom if not geom.is_empty else None
    if isinstance(geom, MultiPolygon):
        polys = [g for g in geom.geoms if isinstance(g, Polygon) and not g.is_empty]
        if not polys:
            return None
        return max(polys, key=lambda p: p.area)
    # GeometryCollection or anything else with mixed geometry types.
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]
    if not polys:
        return None
    return max(polys, key=lambda p: p.area)


def _shift_to_origin(coords: Sequence[Coordinate]) -> List[Coordinate]:
    """Translate a vertex list so its bounding box's min corner sits at
    (0, 0) — matches `from_rectangle`'s convention (room starts at the
    origin) so a preset shape drops into the same positive-coordinate
    workspace as every other shape instead of straddling negative x/y."""
    min_x = min(x for x, _ in coords)
    min_y = min(y for _, y in coords)
    return [(x - min_x, y - min_y) for x, y in coords]


@dataclass
class RoomShape:
    """A validated, correctly-oriented room polygon (with optional
    interior holes for obstacles like columns/cores). Construct via
    `from_rectangle` or `from_vertices` / `from_spec` — not directly —
    so every instance has already been through `_validate`."""

    polygon: Polygon

    # -- construction ------------------------------------------------------

    @classmethod
    def from_rectangle(cls, width: float, length: float) -> "RoomShape":
        if width <= 0 or length <= 0:
            raise RoomShapeError(
                f"Rectangle dimensions must be positive (got width={width}, length={length})."
            )
        polygon = Polygon([(0, 0), (width, 0), (width, length), (0, length)])
        return cls(polygon=polygon)._validated()

    @classmethod
    def from_vertices(
        cls,
        exterior: Sequence[Coordinate],
        holes: Optional[Sequence[Sequence[Coordinate]]] = None,
    ) -> "RoomShape":
        if len(exterior) < 3:
            raise RoomShapeError(
                f"A room shape needs at least 3 vertices (got {len(exterior)})."
            )
        holes = holes or []
        for i, hole in enumerate(holes):
            if len(hole) < 3:
                raise RoomShapeError(
                    f"Hole {i} needs at least 3 vertices (got {len(hole)})."
                )
        polygon = Polygon(shell=list(exterior), holes=[list(h) for h in holes])
        return cls(polygon=polygon)._validated()

    @classmethod
    def from_regular_polygon(
        cls,
        sides: int,
        radius: Optional[float] = None,
        side_length: Optional[float] = None,
        rotation_deg: float = 0.0,
    ) -> "RoomShape":
        """A regular N-gon (equal sides/angles) — triangle, pentagon,
        hexagon, octagon, etc. are all just this with a different
        `sides`, so one code path covers all of "the normal polygons"
        rather than a hand-written vertex list per shape name.

        Give either `radius` (circumradius — center to each corner) or
        `side_length` (edge length); exactly one is required. `rotation_deg`
        spins the whole shape about its center (0 = one corner points
        straight up), useful for lining an edge up flat against a wall.
        """
        if sides < 3:
            raise RoomShapeError(f"A polygon needs at least 3 sides (got {sides}).")
        if (radius is None) == (side_length is None):
            raise RoomShapeError("Give exactly one of 'radius' or 'side_length', not both/neither.")
        if radius is None:
            if side_length <= 0:
                raise RoomShapeError(f"side_length must be positive (got {side_length}).")
            radius = side_length / (2 * math.sin(math.pi / sides))
        elif radius <= 0:
            raise RoomShapeError(f"radius must be positive (got {radius}).")

        start = math.radians(rotation_deg) - math.pi / 2
        exterior = [
            (radius * math.cos(start + 2 * math.pi * i / sides),
             radius * math.sin(start + 2 * math.pi * i / sides))
            for i in range(sides)
        ]
        return cls.from_vertices(_shift_to_origin(exterior))

    @classmethod
    def from_trapezoid(
        cls,
        bottom_width: float,
        top_width: float,
        height: float,
    ) -> "RoomShape":
        """An isosceles trapezoid: `bottom_width` along y=0, `top_width`
        along y=height, both centered on the same vertical axis. A
        rectangle is the special case bottom_width == top_width, and a
        triangle the special case top_width == 0 — no need to special-case
        either, `from_vertices` handles the degenerate-corner triangle
        case fine as long as top_width isn't so small the shape collapses.
        """
        for name, value in (("bottom_width", bottom_width), ("height", height)):
            if value <= 0:
                raise RoomShapeError(f"{name} must be positive (got {value}).")
        if top_width < 0:
            raise RoomShapeError(f"top_width must be zero or positive (got {top_width}).")
        exterior = [
            (-bottom_width / 2, 0.0),
            (bottom_width / 2, 0.0),
            (top_width / 2, height),
            (-top_width / 2, height),
        ]
        return cls.from_vertices(_shift_to_origin(exterior))

    @classmethod
    def from_ellipse(
        cls,
        radius_x: float,
        radius_y: float,
        segments: int = 64,
        rotation_deg: float = 0.0,
    ) -> "RoomShape":
        """A circle (radius_x == radius_y) or oval, approximated as a
        many-sided polygon since shapely/room_geometry only ever deals in
        straight-edge polygons — `segments` controls how smooth that
        approximation is (64 is visually indistinguishable from a true
        ellipse at room scale; raise it for a very large room, lower it
        if a huge segment count is making downstream ops slow).
        """
        if radius_x <= 0 or radius_y <= 0:
            raise RoomShapeError(
                f"radius_x and radius_y must be positive (got radius_x={radius_x}, radius_y={radius_y})."
            )
        if segments < 8:
            raise RoomShapeError(f"segments must be at least 8 for a usable oval/circle (got {segments}).")
        rot = math.radians(rotation_deg)
        cos_r, sin_r = math.cos(rot), math.sin(rot)
        exterior = []
        for i in range(segments):
            theta = 2 * math.pi * i / segments
            ex, ey = radius_x * math.cos(theta), radius_y * math.sin(theta)
            # Rotate the (unrotated) ellipse point about the origin.
            exterior.append((ex * cos_r - ey * sin_r, ex * sin_r + ey * cos_r))
        return cls.from_vertices(_shift_to_origin(exterior))

    @classmethod
    def from_spec(cls, spec: dict) -> "RoomShape":
        """Build a RoomShape from the JSON shape spec the frontend/API
        sends around: either `{"type": "rectangle", "width":, "length":}`
        or `{"type": "polygon", "exterior": [[x,y], ...], "holes": [[[x,y], ...], ...]}`.
        Centralized here so `api_routes.py` never has to know the two
        shapes of that dict itself.
        """
        if not isinstance(spec, dict):
            raise RoomShapeError("Shape definition must be a JSON object.")

        shape_type = str(spec.get("type", "rectangle")).lower()

        if shape_type == "rectangle":
            try:
                width = float(spec["width"])
                length = float(spec["length"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RoomShapeError(
                    "Rectangle shape needs numeric 'width' and 'length'."
                ) from exc
            return cls.from_rectangle(width, length)

        if shape_type == "polygon":
            raw_exterior = spec.get("exterior")
            if not raw_exterior:
                raise RoomShapeError("Polygon shape needs an 'exterior' vertex list.")
            try:
                exterior = [(float(x), float(y)) for x, y in raw_exterior]
                holes = [
                    [(float(x), float(y)) for x, y in hole]
                    for hole in (spec.get("holes") or [])
                ]
            except (TypeError, ValueError) as exc:
                raise RoomShapeError(
                    "Polygon vertices must be [x, y] number pairs."
                ) from exc
            return cls.from_vertices(exterior, holes)

        if shape_type in ("regular_polygon", "polygon_preset"):
            try:
                sides = int(spec["sides"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RoomShapeError("Regular polygon shape needs an integer 'sides'.") from exc
            radius = spec.get("radius")
            side_length = spec.get("side_length")
            try:
                radius = float(radius) if radius is not None else None
                side_length = float(side_length) if side_length is not None else None
            except (TypeError, ValueError) as exc:
                raise RoomShapeError("'radius'/'side_length' must be numbers.") from exc
            rotation_deg = float(spec.get("rotation_deg", 0.0))
            return cls.from_regular_polygon(sides, radius=radius, side_length=side_length, rotation_deg=rotation_deg)

        if shape_type == "trapezoid":
            try:
                bottom_width = float(spec["bottom_width"])
                top_width = float(spec["top_width"])
                height = float(spec["height"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RoomShapeError(
                    "Trapezoid shape needs numeric 'bottom_width', 'top_width' and 'height'."
                ) from exc
            return cls.from_trapezoid(bottom_width, top_width, height)

        if shape_type == "circle":
            diameter = spec.get("diameter")
            radius = spec.get("radius")
            try:
                if radius is not None:
                    radius = float(radius)
                elif diameter is not None:
                    radius = float(diameter) / 2
                else:
                    raise KeyError("radius")
            except (TypeError, ValueError) as exc:
                raise RoomShapeError("Circle shape needs a numeric 'radius' (or 'diameter').") from exc
            except KeyError as exc:
                raise RoomShapeError("Circle shape needs a numeric 'radius' (or 'diameter').") from exc
            segments = int(spec.get("segments", 64))
            return cls.from_ellipse(radius, radius, segments=segments)

        if shape_type in ("oval", "ellipse"):
            def _dim(key_radius, key_full):
                r = spec.get(key_radius)
                full = spec.get(key_full)
                if r is not None:
                    return float(r)
                if full is not None:
                    return float(full) / 2
                raise RoomShapeError(
                    f"Oval shape needs numeric '{key_radius}' (or '{key_full}')."
                )
            try:
                radius_x = _dim("radius_x", "width")
                radius_y = _dim("radius_y", "height")
            except (TypeError, ValueError) as exc:
                raise RoomShapeError("Oval dimensions must be numbers.") from exc
            segments = int(spec.get("segments", 64))
            rotation_deg = float(spec.get("rotation_deg", 0.0))
            return cls.from_ellipse(radius_x, radius_y, segments=segments, rotation_deg=rotation_deg)

        raise RoomShapeError(
            f"Unknown shape type '{shape_type}' (expected 'rectangle', 'polygon', "
            "'regular_polygon', 'trapezoid', 'circle', or 'oval')."
        )

    # -- validation ----------------------------------------------------------

    def _validated(self) -> "RoomShape":
        """Fix self-intersections/orientation, or fail with a clear
        message rather than letting a bad polygon silently produce a
        wrong area or contains() result later. See §5 of the plan."""
        polygon = self.polygon

        if not polygon.is_valid:
            reason = explain_validity(polygon)
            repaired = _largest_polygon(make_valid(polygon))
            if repaired is None or repaired.is_empty:
                raise RoomShapeError(f"Room shape is not a valid simple polygon: {reason}")
            polygon = repaired

        if polygon.is_empty or polygon.area <= 0:
            raise RoomShapeError("Room shape has zero or negative area.")

        # Normalize winding: CCW exterior, CW holes. Doesn't change the
        # geometry, just makes orientation-sensitive downstream code (if
        # any gets added later) safe to assume a convention.
        polygon = orient(polygon, sign=1.0)

        self.polygon = polygon
        return self

    # -- queries used by fixture_layout.py / grid_generator.py -------------

    def contains(self, x: float, y: float) -> bool:
        """True if (x, y) is inside the room and outside every hole,
        INCLUDING points exactly on an edge. Uses `covers` rather than
        shapely's stricter `contains` on purpose: `contains` excludes
        boundary points, which would silently drop every grid/lattice
        point that lands exactly on an eroded region's edge (a common
        case, since the grid/lattice generation always includes the
        region's own bounding-box edges) - see grid_generator.py and
        fixture_layout.py, which rely on this same "boundary counts"
        semantics via their own `.covers(...)` calls on eroded regions."""
        return self.polygon.covers(Point(x, y))

    def eroded(self, margin: float) -> BaseGeometry:
        """The region at least `margin` meters inside every wall/hole
        edge — used for both fixture placement and measurement-point
        clearance. Returns a shapely geometry (Polygon or, for a concave
        shape whose erosion splits it into pieces, a MultiPolygon) —
        callers only need `.contains(Point(...))` and `.bounds`, both of
        which work the same way on either type, so nothing downstream
        needs to special-case the split-polygon case.
        """
        if margin <= 0:
            return self.polygon
        # join_style=2 (mitre) keeps straight walls straight instead of
        # rounding corners, which matters for a small margin on a
        # rectangular or angular room.
        return self.polygon.buffer(-margin, join_style=2)

    def area(self) -> float:
        return self.polygon.area

    def perimeter(self) -> float:
        """Exterior wall length only (holes' own perimeter, e.g. an
        interior courtyard, isn't counted as room wall area) - used by
        interreflection.py to estimate total wall surface area as
        perimeter * ceiling_height."""
        return self.polygon.exterior.length

    def bounds(self) -> Tuple[float, float, float, float]:
        """(minx, miny, maxx, maxy) of the room's outer boundary."""
        return self.polygon.bounds

    def exterior_coords(self) -> List[Coordinate]:
        return list(self.polygon.exterior.coords)

    def hole_coords(self) -> List[List[Coordinate]]:
        return [list(interior.coords) for interior in self.polygon.interiors]

    def to_dict(self) -> dict:
        """Normalized JSON-friendly description for the frontend to draw
        (returned by `POST /api/shape` and embedded in `/api/layout` and
        `/api/calculate` responses)."""
        minx, miny, maxx, maxy = self.bounds()
        return {
            "area": self.area(),
            "bounds": {"min_x": minx, "min_y": miny, "max_x": maxx, "max_y": maxy},
            "exterior": [list(coord) for coord in self.exterior_coords()],
            "holes": [[list(coord) for coord in hole] for hole in self.hole_coords()],
        }
