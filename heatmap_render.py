"""
heatmap_render.py
------------------
Converts a list of (x, y, value) grid points into the JSON structure the
frontend <canvas> needs to draw a heatmap directly - including a
precomputed hex color per point, so the JS side stays dumb (just "draw
this rect in this color") rather than reimplementing a color-scale.

Deliberately format-only: no PNG/image encoding here. If a static image
export is wanted later, that's a separate function built on top of this
one, not a change to it.
"""

from typing import Dict, List, Tuple

from config import HEATMAP_COLOR_STOPS
from models import GridPoint


def _interpolate_color(fraction: float, stops: List[Tuple[float, Tuple[int, int, int]]]) -> Tuple[int, int, int]:
    """Linear interpolation between color stops. `fraction` is clamped to
    [0, 1] before lookup."""
    fraction = min(1.0, max(0.0, fraction))

    for (pos_a, color_a), (pos_b, color_b) in zip(stops, stops[1:]):
        if pos_a <= fraction <= pos_b:
            span = pos_b - pos_a
            local_frac = (fraction - pos_a) / span if span > 0 else 0.0
            r = color_a[0] + (color_b[0] - color_a[0]) * local_frac
            g = color_a[1] + (color_b[1] - color_a[1]) * local_frac
            b = color_a[2] + (color_b[2] - color_a[2]) * local_frac
            return int(round(r)), int(round(g)), int(round(b))

    # Fraction landed exactly on the last stop (or stops list has 1 entry).
    return stops[-1][1]


def _rgb_to_hex(rgb: Tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def value_to_color(value: float, vmin: float, vmax: float) -> str:
    """Public helper in case a caller wants a single color without
    building a full grid payload."""
    span = vmax - vmin
    fraction = (value - vmin) / span if span > 0 else 0.0
    return _rgb_to_hex(_interpolate_color(fraction, HEATMAP_COLOR_STOPS))


def render_grid(points: List[GridPoint]) -> Dict:
    """Build the JSON-serializable payload the frontend consumes.

    Returns:
        {
            "points": [{"x":.., "y":.., "value":.., "color": "#rrggbb"}, ...],
            "vmin": ..., "vmax": ...,
        }
    """
    values = [p.value for p in points]
    vmin, vmax = min(values), max(values)

    return {
        "points": [
            {
                "x": p.x,
                "y": p.y,
                "value": p.value,
                "color": value_to_color(p.value, vmin, vmax),
            }
            for p in points
        ],
        "vmin": vmin,
        "vmax": vmax,
    }
