"""
interreflection.py
-------------------
Approximate room-average interreflected (indirect) illuminance component —
light bounced off the ceiling/walls/floor, which the point-by-point direct
calc in photometry.py deliberately excludes (see that module's docstring:
"E = I(theta) * cos(theta) / d^2" is a direct-only formula).

--- v3: spatial redistribution of the wall-reflection term -------------
v2 (see the history below) fixed E_avg but still added the *entire*
indirect component — ceiling-bounce AND wall-bounce — as one flat number
applied uniformly to every grid point. Validated against DIALux, that
flat approach undershot U0 (0.774 vs DIALux's 0.850 on the 100x120m/7.8m
test room) because it can't give corner/near-wall points the extra boost
they really get from being close to two reflecting walls at once —
DIALux's own per-point radiosity does exactly that.

v3 keeps the ceiling-bounce term flat (physically reasonable: the
ceiling is large relative to any point's distance to it, so its
contribution barely varies across the floor) but replaces the flat
WALL-bounce term with a per-point value from `spatial_wall_indirect()`,
computed by treating each of the room's four walls as a finite,
uniformly-emitting Lambertian rectangle and integrating its point-by-
point contribution to the floor (closed-form along the wall's length,
Simpson's rule up its height).

IMPORTANT — this redistribution is energy-preserving BY CONSTRUCTION:
the raw per-point wall values are rescaled so their GRID AVERAGE exactly
equals the wall_component_avg the v2 solve already produced (already
validated to reproduce DIALux's E_avg to within ~0.1%). Redistributing a
fixed total across points differently changes the SPREAD (raising E_min
at corners must be paired with slightly lower values elsewhere so the
mean is unchanged) — it cannot move E_avg. That also means the
`spatial_weight` knob below only trades off U0 accuracy; it is inert
with respect to E_avg by design — verified empirically (E_avg identical
to 0.1 lux across spatial_weight = 0.0 through 1.0 on the test room).

`spatial_weight` (0..1) blends flat and fully-redistributed wall
indirect. At 1.0 (full redistribution) this tool's simplified single-
generation, uniform-exitance-over-the-whole-wall-height model was found
to OVERSHOOT DIALux's U0 (0.920 vs 0.850) on the validated test room —
real radiosity spreads the wall's re-reflected flux more than this
shortcut does (further bounce generations, non-uniform exitance up the
wall's own height, DIALux's own coarser calc mesh smoothing it out).
spatial_weight ~= 0.45 matched that test case closely. This is flagged
as an EMPIRICAL damping factor, not a derived physical constant —
recheck it against DIALux (or against direct-only, interreflection off)
for a meaningfully different room shape/aspect ratio/reflectance set
before trusting the default elsewhere.

--- v2: why THAT was rewritten (kept for history) -----------------------
v1 used the classic "well-mixed cavity" / Sumpner's-principle shortcut:

    E_indirect = (Phi_total * rho_avg) / (A_surfaces * (1 - rho_avg))

which treats floor + ceiling + walls as ONE averaged, uniformly-mixed
enclosure (like an integrating sphere) and derives its own implicit
"first-hit" baseline as Phi_total / A_surfaces. That baseline is a
*different, smaller* number than the real average direct illuminance this
tool already computes point-by-point on the actual floor area (which is
usually much smaller than floor+ceiling+wall combined) — mixing the two
bases is what caused the ~2x overshoot found when validating against
DIALux on a large, low-ceiling room (high "room index"). v2 replaced it
with a 3-surface radiosity network (floor/ceiling/wall as diffuse nodes,
connected by real geometric view factors) grounded in the tool's own
measured direct-illuminance average instead of a re-derived idealized
flux density.
"""

import math
from dataclasses import dataclass
from typing import Iterable, List

import photometry
from models import Fixture, GridPoint
from room_geometry import RoomShape


def _parallel_plate_view_factor(width: float, length: float, gap: float) -> float:
    """View factor between two identical, directly-opposed parallel
    rectangles (floor -> ceiling), given the room's plan dimensions and
    the vertical gap between them. Standard closed-form result (see e.g.
    Incropera's Fundamentals of Heat and Mass Transfer, table of
    radiation view factors, "aligned parallel rectangles").

    For a non-rectangular room, `width`/`length` should be the axis-
    aligned bounding-box dimensions — an approximation, but a reasonable
    one: it's the floor/ceiling coupling that matters here, and most real
    rooms are close enough to their bounding rectangle for this term.
    """
    if gap <= 0 or width <= 0 or length <= 0:
        return 0.0

    X, Y = width / gap, length / gap
    a = math.sqrt(1.0 + X * X)
    b = math.sqrt(1.0 + Y * Y)
    inner = (1.0 + X * X) * (1.0 + Y * Y) / (1.0 + X * X + Y * Y)
    term = (
        math.log(math.sqrt(max(inner, 1e-12)))
        + X * b * math.atan(X / b)
        + Y * a * math.atan(Y / a)
        - X * math.atan(X)
        - Y * math.atan(Y)
    )
    f = (2.0 / (math.pi * X * Y)) * term
    return min(max(f, 0.0), 1.0)


def _solve_3x3(matrix, rhs):
    """Cramer's-rule solve for the 3x3 radiosity system — small and fixed
    size, so this avoids adding a numpy dependency for one linear solve."""
    (a, b, c), (d, e, f), (g, h, i) = matrix
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        return (0.0, 0.0, 0.0)

    def replace_col(col_index):
        m = [list(matrix[0]), list(matrix[1]), list(matrix[2])]
        for row in range(3):
            m[row][col_index] = rhs[row]
        (a2, b2, c2), (d2, e2, f2), (g2, h2, i2) = m
        return a2 * (e2 * i2 - f2 * h2) - b2 * (d2 * i2 - f2 * g2) + c2 * (d2 * h2 - e2 * g2)

    x = replace_col(0) / det
    y = replace_col(1) / det
    z = replace_col(2) / det
    return (x, y, z)


@dataclass
class IndirectComponents:
    """Result of the room-average 3-surface radiosity solve, split so the
    caller can apply the ceiling term flat and the wall term spatially.

    `ceiling_component` (lux) — floor<->ceiling bounce, applied uniformly
    to every grid point (see module docstring for why this one stays
    flat).

    `wall_component_avg` (lux) — floor<->wall bounce, room-average. Add
    this flat for a v2-equivalent result, OR pass this object into
    `spatial_wall_indirect()` for the v3 per-point redistribution — its
    grid average is guaranteed to equal this value, so switching between
    flat and spatial never changes E_avg.

    `wall_exitance` (lm/m^2) — the walls' own outgoing flux density
    (rho_wall * incident flux / wall area), needed by
    `spatial_wall_indirect()` to compute each wall's contribution as a
    Lambertian source; kept here rather than recomputed from scratch.
    """

    ceiling_component: float
    wall_component_avg: float
    wall_exitance: float


def solve_indirect_components(
    fixtures: Iterable[Fixture],
    shape: RoomShape,
    ceiling_height: float,
    ceiling_reflectance: float,
    wall_reflectance: float,
    floor_reflectance: float,
    direct_avg_illuminance: float,
) -> IndirectComponents:
    """Solve the 3-surface (floor/ceiling/wall) radiosity network.

    `direct_avg_illuminance` is the average of the DIRECT-ONLY point calc
    across the measurement grid (raw, before maintenance factor) — the
    caller must compute this first (see api_routes.py) and pass it in, so
    the radiosity network starts from the same physical quantity the rest
    of the tool already trusts, instead of re-deriving a "first-hit"
    baseline from luminaire flux and room area (v1's bug).

    `ceiling_height` is the interior height used for the wall-area /
    view-factor estimate — pass the actual ceiling height if fixtures are
    pendant-mounted below it (the caller defaults this to mounting_height
    when the user hasn't set one, which is correct only for flush/
    surface-mounted fixtures — get this wrong and BOTH components below
    come out wrong, since the whole geometry shifts).

    Returns an all-zero result for a degenerate room (zero area/height)
    rather than raising — an indirect component being unavailable
    shouldn't block the direct calculation the rest of the tool still
    produces.
    """
    floor_area = shape.area()
    ceiling_area = floor_area
    wall_area = shape.perimeter() * max(ceiling_height, 0.0)

    if floor_area <= 0 or ceiling_height <= 0 or wall_area <= 0:
        return IndirectComponents(0.0, 0.0, 0.0)

    minx, miny, maxx, maxy = shape.bounds()
    bbox_width = max(maxx - minx, 1e-6)
    bbox_length = max(maxy - miny, 1e-6)

    rho_f = min(max(floor_reflectance, 0.0), 0.95)
    rho_c = min(max(ceiling_reflectance, 0.0), 0.95)
    rho_w = min(max(wall_reflectance, 0.0), 0.95)

    # Geometric view factors: floor<->ceiling from the closed-form
    # parallel-plate result, everything else from reciprocity + the
    # "a flat surface's view factors sum to 1" identity.
    f_fc = _parallel_plate_view_factor(bbox_width, bbox_length, ceiling_height)
    f_cf = f_fc
    f_fw = 1.0 - f_fc
    f_cw = 1.0 - f_cf
    f_wf = (floor_area * f_fw) / wall_area
    f_wc = (ceiling_area * f_cw) / wall_area

    # First-generation ("direct-hit") flux on each surface. The floor's
    # is exactly what the point grid already measured — no re-deriving
    # it. Fixtures here are assumed aimed downward with negligible
    # uplight (matches this tool's v1 photometry assumption), so nothing
    # hits the ceiling directly; whatever total luminaire flux the floor
    # measurement DIDN'T account for is treated as spill that landed on
    # the walls directly (near-perimeter fixtures, wide beam angles).
    phi_total = sum(
        photometry.total_flux(fixture.ies) * fixture.flux_scale
        for fixture in fixtures
    )
    phi_floor_direct = direct_avg_illuminance * floor_area
    phi_ceiling_direct = 0.0
    phi_wall_direct = max(phi_total - phi_floor_direct, 0.0)

    # Equilibrium incident flux on each surface (3-node radiosity):
    #   Phi_F_in = Phi_F_direct + rho_C * Phi_C_in * F_cf + rho_W * Phi_W_in * F_wf
    #   Phi_C_in = Phi_C_direct + rho_F * Phi_F_in * F_fc + rho_W * Phi_W_in * F_wc
    #   Phi_W_in = Phi_W_direct + rho_F * Phi_F_in * F_fw + rho_C * Phi_C_in * F_cw
    matrix = (
        (1.0, -rho_c * f_cf, -rho_w * f_wf),
        (-rho_f * f_fc, 1.0, -rho_w * f_wc),
        (-rho_f * f_fw, -rho_c * f_cw, 1.0),
    )
    rhs = (phi_floor_direct, phi_ceiling_direct, phi_wall_direct)
    _phi_floor_in, phi_ceiling_in, phi_wall_in = _solve_3x3(matrix, rhs)
    phi_ceiling_in = max(phi_ceiling_in, 0.0)
    phi_wall_in = max(phi_wall_in, 0.0)

    ceiling_component = (rho_c * phi_ceiling_in * f_cf) / floor_area
    wall_component_avg = (rho_w * phi_wall_in * f_wf) / floor_area
    wall_exitance = (rho_w * phi_wall_in) / wall_area

    return IndirectComponents(
        ceiling_component=max(ceiling_component, 0.0),
        wall_component_avg=max(wall_component_avg, 0.0),
        wall_exitance=max(wall_exitance, 0.0),
    )


def average_indirect_illuminance(
    fixtures: Iterable[Fixture],
    shape: RoomShape,
    ceiling_height: float,
    ceiling_reflectance: float,
    wall_reflectance: float,
    floor_reflectance: float,
    direct_avg_illuminance: float,
) -> float:
    """Flat (v2-equivalent) total indirect illuminance — kept for callers
    that don't need the spatial wall breakdown (e.g. a quick single-
    number preview). The main /api/calculate route uses
    `solve_indirect_components` + `spatial_wall_indirect` instead so the
    wall term can be redistributed; this wrapper just adds both flat
    components together for the same total.
    """
    components = solve_indirect_components(
        fixtures, shape, ceiling_height, ceiling_reflectance,
        wall_reflectance, floor_reflectance, direct_avg_illuminance,
    )
    return components.ceiling_component + components.wall_component_avg


def _wall_rect_point_illuminance(a: float, b_lo: float, b_hi: float, height: float, exitance: float, z_steps: int = 12) -> float:
    """Illuminance at a floor point from ONE finite vertical Lambertian
    wall rectangle (height `height`, uniform exitance `exitance`
    lm/m^2), where `a` is the point's perpendicular distance from the
    wall's plane and [b_lo, b_hi] is the wall's along-wall extent
    expressed relative to the point's own projection onto that line
    (i.e. pass wall_start - point_coord, wall_end - point_coord).

    Closed-form in the along-wall direction (standard integral of
    y/(k^2+y^2)^2), Simpson's rule up the wall's height — this keeps the
    per-point cost to a handful of evaluations instead of a full 2D
    numeric double-integral.
    """
    if a <= 1e-9 or height <= 0 or exitance <= 0:
        return 0.0

    def y_integral(z: float) -> float:
        k2 = a * a + z * z
        k = math.sqrt(k2)

        def antideriv(y: float) -> float:
            return y / (k2 + y * y) + (1.0 / k) * math.atan(y / k)

        return (antideriv(b_hi) - antideriv(b_lo)) / (2.0 * k2)

    n = z_steps if z_steps % 2 == 0 else z_steps + 1
    step = height / n
    total = 0.0
    for i in range(n + 1):
        z = i * step
        weight = 1 if i in (0, n) else (4 if i % 2 == 1 else 2)
        total += weight * z * y_integral(z)
    integral_z = total * step / 3.0  # Simpson's rule

    return (exitance * a / math.pi) * integral_z


def spatial_wall_indirect(
    points: Iterable["GridPoint"],
    shape: RoomShape,
    ceiling_height: float,
    components: IndirectComponents,
    spatial_weight: float = 0.45,
) -> List[float]:
    """Per-point wall-reflection indirect illuminance (lux), replacing
    the flat `components.wall_component_avg` with a spatially-varying
    version that's stronger near walls/corners and weaker toward the
    room's center — matching how DIALux's real per-point radiosity
    behaves, instead of adding the same number everywhere.

    Rectangle-bounding-box approximation, same as the ceiling view
    factor: each of the room's 4 bounding-box walls is treated as one
    finite, uniformly-emitting Lambertian rectangle using
    `components.wall_exitance`.

    ENERGY-PRESERVING BY CONSTRUCTION: the raw per-point values are
    rescaled so their average over `points` exactly equals
    `components.wall_component_avg` before the `spatial_weight` blend is
    applied — so this can only change U0/U1 (the spread), never E_avg.
    `spatial_weight=0.0` reproduces the flat v2 behavior exactly;
    `spatial_weight=1.0` is the fully-redistributed (currently over-
    corrected on the validated test case, see module docstring) result.

    Returns [] (caller should fall back to the flat component) if the
    wall component is zero/negligible or the room is degenerate — this
    keeps a zero-reflectance or zero-height edge case cheap instead of
    doing real geometry work for a result that's zero anyway.
    """
    points = list(points)
    if not points or components.wall_exitance <= 0 or components.wall_component_avg <= 0:
        return []

    minx, miny, maxx, maxy = shape.bounds()

    raw_values = []
    for pt in points:
        x, y = pt.x, pt.y
        total = 0.0
        # West wall (x = minx), spans y in [miny, maxy]; East wall mirrors it.
        total += _wall_rect_point_illuminance(x - minx, miny - y, maxy - y, ceiling_height, components.wall_exitance)
        total += _wall_rect_point_illuminance(maxx - x, miny - y, maxy - y, ceiling_height, components.wall_exitance)
        # South wall (y = miny), spans x in [minx, maxx]; North wall mirrors it.
        total += _wall_rect_point_illuminance(y - miny, minx - x, maxx - x, ceiling_height, components.wall_exitance)
        total += _wall_rect_point_illuminance(maxy - y, minx - x, maxx - x, ceiling_height, components.wall_exitance)
        raw_values.append(total)

    avg_raw = sum(raw_values) / len(raw_values)
    if avg_raw <= 0:
        return []

    scale = components.wall_component_avg / avg_raw
    weight = min(max(spatial_weight, 0.0), 1.0)
    return [
        components.wall_component_avg * (1.0 - weight) + (v * scale) * weight
        for v in raw_values
    ]
