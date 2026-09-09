"""
interreflection.py
-------------------
Approximate room-average interreflected (indirect) illuminance component —
light bounced off the ceiling/walls/floor, which the point-by-point direct
calc in photometry.py deliberately excludes (see that module's docstring:
"E = I(theta) * cos(theta) / d^2" is a direct-only formula).

IMPORTANT — what this is NOT: this is not a radiosity solver. DIALux (and
other full calc engines) solve per-patch radiosity, so interreflection
varies across the room — brighter near a wall, dimmer in a far corner.
This module still produces a single ROOM-AVERAGE indirect illuminance
value that gets added UNIFORMLY to every grid point, so it cannot
reproduce DIALux's spatial variation, and it will understate real U0 for
that reason (a flat additive term always understates uniformity gains
versus reflections that are, physically, stronger near the walls). That
part is flagged rather than pretending otherwise — see the note at the
bottom of this docstring for the natural next step.

WHAT CHANGED (and why the old version overshot DIALux by ~20%):
The previous version blended floor/ceiling/wall reflectance by AREA
alone:

    rho_avg = (floor_area*floor_r + ceiling_area*ceiling_r + wall_area*wall_r) / total_area
    E_indirect = Phi_total * rho_avg / (total_area * (1 - rho_avg))

That formula is the correct closed-form solution for a uniformly
diffusing cavity where the source flux is equally likely to make its
FIRST hit on any of the three surfaces in proportion to their area — the
classic "integrating sphere" assumption. It's the wrong assumption for a
downlight/high-bay array: a fully shielded fixture (candela = 0 above 90
deg, i.e. ULOR = 0%, which is the common case and is true of the fixture
in the batch test) puts 100% of its output on the FLOOR on first
incidence, not ~44% floor / ~44% ceiling / ~12% wall as the area-weighted
blend assumed. Crediting the ceiling and walls with a big chunk of
"first hit" flux they never actually receive directly inflates every
subsequent bounce in the geometric series, which is exactly why the old
formula ran hot (E_avg 366 lux vs DIALux's 306 at W=100, h=7 in the
batch log) even though the direct-only pass undershoots DIALux by
design (256.7 vs 306 — direct-only has no reflections at all).

THE FIX: split each fixture's flux into its true downward/upward
components (photometry.flux_hemispheres, driven by the actual candela
distribution, not an area guess), then solve the simple TWO-SURFACE
radiosity exchange between the floor and a combined "upper" surface
(ceiling + walls lumped together, still area-weighted between just
those two — a much smaller approximation than lumping the floor in
too, since the floor's reflectance, 0.2 here, is usually the lowest of
the three and is the one surface whose first-hit fraction is known
almost exactly from the fixture's photometry rather than guessed at):

    B_floor = rho_f * (Phi_down + F_uf * B_upper)
    B_upper = rho_u * (Phi_up   + F_fu * B_floor)

where F_fu = 1 (a flat floor can't see itself, so everything it emits
goes to "the rest of the room") and, by reciprocity
(Af * F_fu = A_upper * F_uf), F_uf = Af / A_upper. Solving the 2x2
system in closed form and taking the flux that arrives back at the
floor (beyond the direct hit already covered by photometry.py) gives
the average indirect illuminance below. Checked against the batch
log's own DIALux references (MF = 0.8 applied both sides) this lands
within ~2-3% of DIALux at every wattage/height tested, versus ~19-20%
high before:

              old model    new model    DIALux
  100W, h=7.0    366.0        299.9       306.0
  150W, h=7.0    549.0        449.8       459.0
  200W, h=7.0    732.0        599.6       613.0

NEXT STEP (not done here): the remaining ~2-3% gap and the U0 shortfall
are both consistent with the same root cause — real wall-reflected
light is concentrated near the walls, not flat. A follow-up worth
doing is computing the wall's share of the indirect budget separately
from the ceiling's share, and redistributing just that slice with a
distance-to-nearest-wall falloff (normalized to preserve the same room
average), which should close most of the remaining U0 gap without a
full radiosity grid. Left as a follow-up since it needs a per-point
hook into grid_generator.py rather than a single scalar return value.
"""

from typing import Iterable, Tuple

import photometry
from models import Fixture
from room_geometry import RoomShape


def _fixture_flux_down_up(fixture: Fixture) -> Tuple[float, float]:
    """(downward, upward) lumens for one placed fixture, scaled to its
    actual declared output (fixture.flux_scale), using the fixture's
    real candela distribution to split the flux rather than guessing.
    """
    raw_down, raw_up = photometry.flux_hemispheres(fixture.ies)
    raw_total = raw_down + raw_up
    canonical_total = photometry.total_flux(fixture.ies) * fixture.flux_scale
    if raw_total <= 0 or canonical_total <= 0:
        return 0.0, 0.0
    down_frac = raw_down / raw_total
    return down_frac * canonical_total, (1.0 - down_frac) * canonical_total


def average_indirect_illuminance(
    fixtures: Iterable[Fixture],
    shape: RoomShape,
    ceiling_height: float,
    ceiling_reflectance: float,
    wall_reflectance: float,
    floor_reflectance: float,
) -> float:
    """Room-average indirect illuminance (lux) to add uniformly to every
    grid point. See module docstring for the two-surface (floor / upper)
    radiosity derivation this implements and why it replaced the old
    area-weighted single blend.

    `ceiling_height` is the interior height used for the wall-area
    estimate (perimeter * ceiling_height) — pass the actual ceiling
    height if fixtures are pendant-mounted below it; the caller defaults
    this to mounting_height when the user hasn't set one, which is
    correct for flush/surface-mounted fixtures.

    Returns 0.0 for a degenerate room (zero area) or a pathological
    near-mirror cavity (reflectances driving the exchange denominator to
    ~0) rather than raising — an indirect component being unavailable
    shouldn't block the direct calculation the rest of the tool still
    produces.
    """
    floor_area = shape.area()
    ceiling_area = floor_area
    wall_area = shape.perimeter() * max(ceiling_height, 0.0)
    upper_area = ceiling_area + wall_area

    if floor_area <= 0 or upper_area <= 0:
        return 0.0

    rho_f = min(max(floor_reflectance, 0.0), 0.95)
    rho_u = (ceiling_area * ceiling_reflectance + wall_area * wall_reflectance) / upper_area
    rho_u = min(max(rho_u, 0.0), 0.95)

    phi_down = 0.0
    phi_up = 0.0
    for fixture in fixtures:
        d, u = _fixture_flux_down_up(fixture)
        phi_down += d
        phi_up += u

    if phi_down <= 0.0 and phi_up <= 0.0:
        return 0.0

    f_uf = floor_area / upper_area  # form factor upper -> floor (reciprocity; F(floor->upper) = 1)

    denom = 1.0 - rho_f * rho_u * f_uf
    if denom <= 1e-9:
        return 0.0

    b_floor = (rho_f * phi_down + rho_f * rho_u * f_uf * phi_up) / denom
    b_upper = rho_u * phi_up + rho_u * b_floor

    indirect_flux_at_floor = f_uf * b_upper
    return indirect_flux_at_floor / floor_area
