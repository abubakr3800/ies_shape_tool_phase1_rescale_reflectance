"""
models.py
---------
Plain dataclasses shared across the backend. No behaviour lives here on
purpose — this file just defines the shape of the data that flows between
`ies_parser.py`, `photometry.py`, `grid_generator.py`, `aggregator.py` and
`heatmap_render.py`, so every module agrees on field names instead of
passing loose dicts around.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class IesData:
    """Everything pulled out of a parsed .ies file that the rest of the
    tool needs. See ies_parser.py for how this gets built."""

    lamp_count: int
    lumens_per_lamp: float          # -1.0 in the file means "absolute photometry"
    multiplier: float                # universal candela multiplying factor
    vertical_angles: List[float]      # degrees, 0 = nadir (straight down)
    horizontal_angles: List[float]     # degrees, azimuth around the fixture
    candela: List[List[float]]          # candela[h_index][v_index]
    ballast_factor: float
    input_watts: float
    source_filename: str
    tilt_supported: bool = True        # False if TILT=INCLUDE data was skipped

    @property
    def total_lumens(self) -> Optional[float]:
        """Total luminaire output = lamp_count * lumens_per_lamp.
        Informational only (display/metadata) — the actual illuminance
        calculation uses the candela matrix directly, per the IES
        convention that candela values are already absolute."""
        if self.lumens_per_lamp is None or self.lumens_per_lamp < 0:
            return None
        return self.lamp_count * self.lumens_per_lamp

    @property
    def max_vertical_angle(self) -> float:
        return self.vertical_angles[-1] if self.vertical_angles else 0.0

    @property
    def max_horizontal_angle(self) -> float:
        return self.horizontal_angles[-1] if self.horizontal_angles else 0.0


@dataclass
class Fixture:
    """A single placed fixture: where it is, how high it's mounted, and
    which parsed IES data it uses. Phase 1 assumes the fixture aims
    straight down (nadir) — see photometry.py docstring for the note on
    adding an aim vector later.

    `flux_scale` is the "declared lumens override" feature: 1.0 means use
    the candela matrix exactly as published in the file. Any other value
    multiplies every candela lookup by that factor, which is exactly what
    DIALux does when you edit a luminaire's "Luminous flux" away from its
    photometric file's own declared total — see api_routes.calculate()
    for how it's computed from a requested `declared_lumens` value."""

    x: float
    y: float
    mounting_height: float
    ies: IesData
    flux_scale: float = 1.0


@dataclass
class GridPoint:
    """One measurement location on the work plane, plus the illuminance
    value computed for it (filled in after calculation)."""

    x: float
    y: float
    value: float = 0.0


@dataclass
class UniformityResult:
    """The five numbers everything else in this tool exists to produce."""

    e_min: float
    e_max: float
    e_avg: float
    u0: float   # E_min / E_avg
    u1: float   # E_min / E_max
