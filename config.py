"""
config.py
---------
Central place for tunable defaults. Nothing in here does any calculation —
it's just numbers other modules import, so changing a default never means
hunting through calculation code.

Phase 1 scope: single fixture, rectangular room only. The wall-margin /
grid-spacing values already anticipate the polygon-clipped version that
`room_geometry.py` will add in a later phase (arbitrary shapes) — the
*meaning* of these numbers doesn't change, only how the point set is
generated.
"""

# --- Measurement grid defaults -------------------------------------------
# Target spacing between calculation points, in meters. The grid is built
# from a *target spacing* rather than a fixed point count (e.g. "10x10") so
# accuracy doesn't silently degrade on a much bigger or smaller room.
DEFAULT_GRID_SPACING_M = 0.5

# Keep calculation points this far inside the room boundary. Matches the
# "avoid walls by 0.5m" convention used in the earlier LuxScale uniformity
# work.
DEFAULT_WALL_MARGIN_M = 0.5

# --- Room / fixture defaults (used to pre-fill the frontend form) --------
DEFAULT_ROOM_WIDTH_M = 10.0
DEFAULT_ROOM_LENGTH_M = 10.0
DEFAULT_MOUNTING_HEIGHT_M = 3.0
DEFAULT_WORK_PLANE_HEIGHT_M = 0.0  # 0 = floor level

# --- Upload limits ----------------------------------------------------------
MAX_IES_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB is generous for a .ies file

# --- Persistent IES storage --------------------------------------------------
# Uploaded/fetched .ies files are saved here (relative to the app root) so
# they survive a Passenger process restart and can be reused without
# re-uploading. Each saved file gets an <id>.ies + <id>.json metadata pair.
IES_STORAGE_DIR = "storage/ies"

# --- Fetch-by-URL limits -----------------------------------------------------
# Same size cap as a direct upload; separate timeout since this is a
# server-side outbound request rather than a browser upload.
IES_FETCH_TIMEOUT_S = 10

# --- Output rescale / maintenance / interreflection defaults ---------------
# DIALux (and most other calc engines) let you override a luminaire's
# declared lumen output, which rescales its candela distribution
# proportionally - this tool now supports the same thing (see
# photometry.total_flux / Fixture.flux_scale). No default override is
# applied unless the user supplies one (None here means "use the file's own
# declared total, i.e. flux_scale = 1.0").
DEFAULT_DECLARED_LUMENS = None

# Typical DIALux "new project" / CIE-recommended room surface reflectances,
# used as the pre-filled defaults for the reflectance inputs. All are
# user-editable per calculation.
DEFAULT_CEILING_REFLECTANCE = 0.70
DEFAULT_WALL_REFLECTANCE = 0.50
DEFAULT_FLOOR_REFLECTANCE = 0.20

# Light loss / maintenance factor applied to the final result (dirt
# accumulation + lamp lumen depreciation over the maintenance cycle).
# 0.8 matches a common DIALux default for a "clean" indoor industrial/
# commercial environment - user-editable per calculation.
DEFAULT_MAINTENANCE_FACTOR = 0.80

# Whether the approximate room-average interreflected (indirect) component
# is added by default. See interreflection.py for what this does and does
# NOT model (uniform average add-on, not per-point radiosity like DIALux).
DEFAULT_INCLUDE_INTERREFLECTION = True

# --- Heatmap color scale ---------------------------------------------------
# Standard low->high "jet-like" gradient. Deliberately NOT the SC brand
# palette — the heatmap is data encoding, brand colors are page chrome.
# Each stop is (position 0..1, (r, g, b)).
HEATMAP_COLOR_STOPS = [
    (0.00, (0, 0, 255)),      # blue   - lowest illuminance
    (0.25, (0, 255, 255)),    # cyan
    (0.50, (0, 255, 0)),      # green
    (0.75, (255, 255, 0)),    # yellow
    (1.00, (255, 0, 0)),      # red    - highest illuminance
]
