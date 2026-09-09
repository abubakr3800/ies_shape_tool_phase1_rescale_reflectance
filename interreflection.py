"""
interreflection.py
-------------------
Approximate room-average interreflected (indirect) illuminance component —
light bounced off the ceiling/walls/floor, which the point-by-point direct
calc in photometry.py deliberately excludes (see that module's docstring:
"E = I(theta) * cos(theta) / d^2" is a direct-only formula).

IMPORTANT — what this is NOT: this is not a radiosity solver. DIALux (and
other full calc engines) solve per-patch radiosity, so interreflection
varies across the room — brighter near a light wall, dimmer in a far
corner. This module instead uses the classical "flux transfer" / infinite-
bounce equilibrium approximation, which produces a single ROOM-AVERAGE
indirect illuminance value that gets added UNIFORMLY to every grid point.
It will get you into the right neighborhood for E_avg and closer to
DIALux's numbers, but it cannot reproduce DIALux's spatial variation from
reflections, and it will slightly overstate uniformity (U0, U1) versus a
real radiosity calc for that reason. Flagged here rather than pretending
otherwise.

The formula (standard multi-bounce equilibrium, sometimes called the
"average illuminance" or "lumen method" approximation in lighting design
texts): each unit of flux that lands on room surfaces gets re-reflected
indefinitely, contributing rho + rho^2 + rho^3 + ... = rho / (1 - rho) of
itself back into the room as indirect flux, spread uniformly:

    E_indirect = (Phi_total * rho_avg) / (A_surfaces * (1 - rho_avg))

where Phi_total is total luminaire output (lm), A_surfaces is total
interior surface area (floor + ceiling + walls, m^2), and rho_avg is the
area-weighted average reflectance of those three surfaces.
"""

from typing import Iterable

import photometry
from models import Fixture
from room_geometry import RoomShape


def average_indirect_illuminance(
    fixtures: Iterable[Fixture],
    shape: RoomShape,
    ceiling_height: float,
    ceiling_reflectance: float,
    wall_reflectance: float,
    floor_reflectance: float,
) -> float:
    """Room-average indirect illuminance (lux) to add uniformly to every
    grid point, given the fixtures already placed (their total output is
    read via photometry.total_flux(fixture.ies) * fixture.flux_scale) and
    the room's floor area / wall area / reflectances.

    `ceiling_height` is the interior height used for the wall-area
    estimate (perimeter * ceiling_height) — pass the actual ceiling
    height if fixtures are pendant-mounted below it; the caller defaults
    this to mounting_height when the user hasn't set one, which is
    correct for flush/surface-mounted fixtures.

    Returns 0.0 for a degenerate room (zero area) rather than raising —
    an indirect component being unavailable shouldn't block the direct
    calculation the rest of the tool still produces.
    """
    floor_area = shape.area()
    ceiling_area = floor_area
    wall_area = shape.perimeter() * max(ceiling_height, 0.0)
    total_area = floor_area + ceiling_area + wall_area

    if total_area <= 0:
        return 0.0

    rho_avg = (
        floor_area * floor_reflectance
        + ceiling_area * ceiling_reflectance
        + wall_area * wall_reflectance
    ) / total_area
    # Guard the (1 - rho) denominator against a pathological rho_avg=1
    # input (100% reflective everywhere), which would divide by zero.
    rho_avg = min(max(rho_avg, 0.0), 0.95)

    phi_total = sum(
        photometry.total_flux(fixture.ies) * fixture.flux_scale
        for fixture in fixtures
    )

    return (phi_total * rho_avg) / (total_area * (1.0 - rho_avg))
