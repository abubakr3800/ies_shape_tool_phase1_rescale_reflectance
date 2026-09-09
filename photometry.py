"""
photometry.py
-------------
The actual light math. Two things live here:

1. `interpolate_candela` — given an IesData's candela matrix, look up the
   (bilinearly interpolated) candela value at an arbitrary vertical/
   horizontal angle, correctly handling fixtures that only publish a
   symmetric slice of angles (quarter/half symmetry), which is the norm
   for round floodlights/highbays.

2. `point_illuminance` / `total_illuminance` — the inverse-square-law +
   cosine-corrected point-by-point method used throughout the earlier
   LuxScale uniformity work (E = I(theta) * cos(theta) / d^2). This is the
   ONE method this tool uses — no analytic/hybrid shortcut formula, see
   the engineering plan for why.

Known v1 limitation: fixtures are assumed to aim straight down (nadir
aligned with -Z) and to have no yaw rotation of their own horizontal-angle
reference frame. Both are called out as extension points in the
engineering plan (an `aim_vector` / `rotation_deg` parameter can be added
to `Fixture` and threaded through here later without changing the public
function signatures much).
"""

import bisect
import math
from typing import Iterable, List, Tuple

from models import Fixture, IesData


def _fold_horizontal_angle(phi_deg: float, horizontal_angles: List[float]) -> float:
    """Most photometric files exploit symmetry and only publish a slice of
    horizontal angles (e.g. 0-90 for a fully symmetric round fixture, or
    0-180 for a fixture symmetric front-to-back). Fold an arbitrary
    incoming azimuth into whatever range the file actually provides.
    """
    phi = phi_deg % 360.0
    max_angle = horizontal_angles[-1] if horizontal_angles else 360.0

    if max_angle <= 90.0 + 1e-6:
        # Quarter symmetry: mirror into 0-90 within each 90-degree quadrant.
        phi = phi % 180.0
        if phi > 90.0:
            phi = 180.0 - phi
        return phi

    if max_angle <= 180.0 + 1e-6:
        # Half symmetry: mirror around 180.
        if phi > 180.0:
            phi = 360.0 - phi
        return phi

    # Full 0-360 published - use as-is.
    return phi


def _interp_1d(angle: float, angles: List[float], index_before: int) -> Tuple[int, int, float]:
    """Return (low_index, high_index, fraction) for linear interpolation
    of `angle` within the sorted `angles` list. Clamps at both ends
    instead of extrapolating."""
    n = len(angles)
    if n == 1:
        return 0, 0, 0.0
    if angle <= angles[0]:
        return 0, 0, 0.0
    if angle >= angles[-1]:
        return n - 1, n - 1, 0.0

    i = index_before
    lo, hi = angles[i], angles[i + 1]
    frac = (angle - lo) / (hi - lo) if hi != lo else 0.0
    return i, i + 1, frac


def interpolate_candela(ies: IesData, vertical_deg: float, horizontal_deg: float) -> float:
    """Bilinear interpolation of the candela matrix at an arbitrary
    (vertical, horizontal) angle pair, in degrees. Returns candela already
    multiplied by the file's universal multiplying factor."""
    v_angles = ies.vertical_angles
    h_angles = ies.horizontal_angles

    v_deg = min(max(vertical_deg, v_angles[0]), v_angles[-1])
    h_deg = _fold_horizontal_angle(horizontal_deg, h_angles)

    v_i = bisect.bisect_right(v_angles, v_deg) - 1
    v_i = max(0, min(v_i, len(v_angles) - 2)) if len(v_angles) > 1 else 0
    v_lo, v_hi, v_frac = _interp_1d(v_deg, v_angles, v_i)

    h_i = bisect.bisect_right(h_angles, h_deg) - 1
    h_i = max(0, min(h_i, len(h_angles) - 2)) if len(h_angles) > 1 else 0
    h_lo, h_hi, h_frac = _interp_1d(h_deg, h_angles, h_i)

    c00 = ies.candela[h_lo][v_lo]
    c01 = ies.candela[h_lo][v_hi]
    c10 = ies.candela[h_hi][v_lo]
    c11 = ies.candela[h_hi][v_hi]

    c0 = c00 + (c01 - c00) * v_frac
    c1 = c10 + (c11 - c10) * v_frac
    candela = c0 + (c1 - c0) * h_frac

    return candela * ies.multiplier


def point_illuminance(fixture: Fixture, point_x: float, point_y: float, work_plane_z: float = 0.0) -> float:
    """Illuminance (lux) contributed by a single fixture at a single
    work-plane point, via E = I(theta) * cos(theta) / d^2.

    `fixture.mounting_height` is the fixture's height above z=0; the
    work plane is at `work_plane_z`. Both are in meters, illuminance in
    lux, matching lumens-in-candela convention of the IES file.
    """
    dx = point_x - fixture.x
    dy = point_y - fixture.y
    vertical_distance = fixture.mounting_height - work_plane_z

    if vertical_distance <= 0:
        # Fixture is at or below the work plane - not a physically sane
        # setup for a downward-facing fixture; contributes nothing rather
        # than dividing by a non-positive distance.
        return 0.0

    horizontal_distance_sq = dx * dx + dy * dy
    d = math.sqrt(horizontal_distance_sq + vertical_distance * vertical_distance)
    if d == 0:
        return 0.0

    cos_theta = vertical_distance / d  # theta measured from nadir (straight down)
    theta_deg = math.degrees(math.acos(min(1.0, max(-1.0, cos_theta))))
    phi_deg = math.degrees(math.atan2(dy, dx)) % 360.0

    candela = interpolate_candela(fixture.ies, theta_deg, phi_deg) * fixture.flux_scale
    return (candela * cos_theta) / (d * d)


def total_illuminance(fixtures: Iterable[Fixture], point_x: float, point_y: float, work_plane_z: float = 0.0) -> float:
    """Sum of every fixture's contribution at one point. This is the only
    function grid_generator.py needs to call per calculation point."""
    return sum(
        point_illuminance(fixture, point_x, point_y, work_plane_z)
        for fixture in fixtures
    )


def flux_hemispheres(ies: IesData) -> Tuple[float, float]:
    """Split total luminous flux (lm) into (downward, upward) components,
    splitting the same candela(theta,phi)*sin(theta) integral
    `_integrate_candela_to_lumens` does at theta = 90 deg (theta is
    measured from nadir in this module's convention, so 0-90 is
    "downward hemisphere", 90-180 is "upward hemisphere").

    This is what lets interreflection.py know how much flux hits the
    floor directly vs. the ceiling directly on first incidence. A fully
    shielded high-bay/downlight (the common case - zero candela above
    90 deg, i.e. ULOR = 0%) should hand 100% of its output to the
    floor's first-bounce budget, not to an area-weighted blend across
    all three room surfaces - see interreflection.py for why that
    distinction is the difference between matching DIALux and
    overshooting it by ~20%.
    """
    v_angles = ies.vertical_angles
    h_angles = ies.horizontal_angles
    if len(v_angles) < 2:
        return 0.0, 0.0

    max_h = h_angles[-1] if h_angles else 0.0
    if len(h_angles) <= 1:
        symmetry_factor = 2 * math.pi
    elif max_h <= 90.0 + 1e-6:
        symmetry_factor = 4.0
    elif max_h <= 180.0 + 1e-6:
        symmetry_factor = 2.0
    else:
        symmetry_factor = 1.0

    def v_integral_bounded(col: List[float], lo: float, hi: float) -> float:
        """Trapezoidal integral of col(theta)*sin(theta) dtheta restricted
        to [lo, hi] degrees, linearly interpolating col at lo/hi when
        they fall inside a published interval instead of on a grid
        point (only matters for files that don't happen to publish a
        sample exactly at 90 deg)."""
        total = 0.0
        for i in range(len(v_angles) - 1):
            a0, a1 = v_angles[i], v_angles[i + 1]
            seg_lo, seg_hi = max(a0, lo), min(a1, hi)
            if seg_hi <= seg_lo:
                continue
            c0, c1 = col[i], col[i + 1]
            frac_lo = (seg_lo - a0) / (a1 - a0) if a1 != a0 else 0.0
            frac_hi = (seg_hi - a0) / (a1 - a0) if a1 != a0 else 0.0
            v_lo = c0 + (c1 - c0) * frac_lo
            v_hi = c0 + (c1 - c0) * frac_hi
            t_lo, t_hi = math.radians(seg_lo), math.radians(seg_hi)
            f_lo, f_hi = v_lo * math.sin(t_lo), v_hi * math.sin(t_hi)
            total += (f_lo + f_hi) / 2.0 * (t_hi - t_lo)
        return total

    v_max = v_angles[-1]

    if len(h_angles) <= 1:
        col = ies.candela[0]
        down = symmetry_factor * v_integral_bounded(col, 0.0, 90.0) * ies.multiplier
        up = symmetry_factor * v_integral_bounded(col, 90.0, v_max) * ies.multiplier
        return down, up

    down_pub = 0.0
    up_pub = 0.0
    for i in range(len(h_angles) - 1):
        p0 = math.radians(h_angles[i])
        p1 = math.radians(h_angles[i + 1])
        dphi = p1 - p0
        d0 = v_integral_bounded(ies.candela[i], 0.0, 90.0)
        d1 = v_integral_bounded(ies.candela[i + 1], 0.0, 90.0)
        down_pub += (d0 + d1) / 2.0 * dphi
        u0 = v_integral_bounded(ies.candela[i], 90.0, v_max)
        u1 = v_integral_bounded(ies.candela[i + 1], 90.0, v_max)
        up_pub += (u0 + u1) / 2.0 * dphi

    return symmetry_factor * down_pub * ies.multiplier, symmetry_factor * up_pub * ies.multiplier


def total_flux(ies: IesData) -> float:
    """The total luminous flux (lm) this file's candela distribution
    represents — the reference point the 'declared lumens' rescale
    feature (Fixture.flux_scale) and the interreflection module's
    Phi_total both need.

    Prefers the header-declared value (lamp_count * lumens_per_lamp) —
    that's the IES-file convention DIALux and every other tool uses as
    the rescale reference, and it should already match what the candela
    matrix itself integrates to for a correctly-built file. Only falls
    back to numerically integrating the candela matrix for the rare
    'absolute photometry' files (lumens_per_lamp == -1) that don't
    declare one.
    """
    if ies.total_lumens is not None:
        return ies.total_lumens
    return _integrate_candela_to_lumens(ies)


def _integrate_candela_to_lumens(ies: IesData) -> float:
    """Numerically integrate candela(theta, phi) * sin(theta) over the
    full sphere via trapezoidal rule on the published angle grid, then
    scale up by whatever symmetry the file exploits (a file that only
    publishes a 0-90 deg quarter of horizontal angles has its integral
    multiplied by 4 to account for the three unpublished, mirror-image
    quadrants, and so on) so the result is the TRUE total output, not
    just the flux over the published slice.
    """
    v_angles = ies.vertical_angles
    h_angles = ies.horizontal_angles
    if len(v_angles) < 2:
        return 0.0

    max_h = h_angles[-1] if h_angles else 0.0
    if len(h_angles) <= 1:
        # Rotationally symmetric - no azimuthal variation published at all.
        symmetry_factor = 2 * math.pi
        h_pairs = [(0.0, 0.0, 1.0)]  # single "slice", weight below is moot
    elif max_h <= 90.0 + 1e-6:
        symmetry_factor = 4.0
        h_pairs = None
    elif max_h <= 180.0 + 1e-6:
        symmetry_factor = 2.0
        h_pairs = None
    else:
        symmetry_factor = 1.0
        h_pairs = None

    def v_integral(h_index: int) -> float:
        total = 0.0
        col = ies.candela[h_index]
        for i in range(len(v_angles) - 1):
            t0 = math.radians(v_angles[i])
            t1 = math.radians(v_angles[i + 1])
            f0 = col[i] * math.sin(t0)
            f1 = col[i + 1] * math.sin(t1)
            total += (f0 + f1) / 2.0 * (t1 - t0)
        return total

    if len(h_angles) <= 1:
        # candela[0] is the only column; symmetry_factor already covers
        # the full 2*pi azimuthal sweep (rotationally symmetric fixture).
        flux = symmetry_factor * v_integral(0) * ies.multiplier
        return flux

    # General case: trapezoidal integration over the published horizontal
    # angles too, then scale by the mirror-symmetry factor.
    published_total = 0.0
    for i in range(len(h_angles) - 1):
        p0 = math.radians(h_angles[i])
        p1 = math.radians(h_angles[i + 1])
        dphi = p1 - p0
        published_total += (v_integral(i) + v_integral(i + 1)) / 2.0 * dphi

    return symmetry_factor * published_total * ies.multiplier
