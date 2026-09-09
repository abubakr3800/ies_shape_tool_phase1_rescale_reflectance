"""
aggregator.py
-------------
Turns a list of per-point illuminance values into the five summary numbers
this whole tool exists to produce. No physics, no geometry - just stats,
which is exactly why it's split out: it should never need to change when
the room-shape or photometry logic changes, and it's trivial to test with
a hand-picked list of numbers.
"""

from typing import List

from models import UniformityResult


def summarize(values: List[float]) -> UniformityResult:
    """E_min, E_max, E_avg, U0 (=E_min/E_avg), U1 (=E_min/E_max).

    Raises ValueError on an empty list rather than returning misleading
    zeros - an empty grid usually means a shape/clipping bug upstream and
    should be loud, not silent.
    """
    if not values:
        raise ValueError("Cannot summarize an empty set of grid values.")

    e_min = min(values)
    e_max = max(values)
    e_avg = sum(values) / len(values)

    u0 = (e_min / e_avg) if e_avg > 0 else 0.0
    u1 = (e_min / e_max) if e_max > 0 else 0.0

    return UniformityResult(e_min=e_min, e_max=e_max, e_avg=e_avg, u0=u0, u1=u1)
