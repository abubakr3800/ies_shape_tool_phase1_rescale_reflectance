"""
api_routes.py
-------------
Flask Blueprint for the tool's API. Every route is a few lines: parse the
request, call the relevant module function(s), return JSON. If you find
yourself writing calculation logic in this file, it belongs in one of the
other modules instead.

In-memory storage: parsed IES data is kept in a module-level dict keyed
by a random id. That's fine for a single-process prototype used by one
person at a time; it is NOT a design for multi-user/production use (no
expiry, no persistence across restarts) - flagged here rather than
pretending otherwise.

PHASE 2 routes (`/api/shape`, `/api/layout`, `/api/calculate`) add
arbitrary-shape, multi-fixture support per the engineering plan §5. They
are stateless with respect to the shape itself - every request carries
its own `shape` spec (rectangle or polygon+holes) rather than referencing
a stored id, since re-validating a polygon from its vertex list is cheap
and this avoids a second in-memory store to keep in sync. The phase-1
single-fixture rectangular route (`/api/heatmap`) is untouched below.
"""

import time
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request

import aggregator
import fixture_layout
import grid_generator
import heatmap_render
import ies_store
import interreflection
import photometry
from config import (
    DEFAULT_CEILING_REFLECTANCE,
    DEFAULT_FLOOR_REFLECTANCE,
    DEFAULT_GRID_SPACING_M,
    DEFAULT_INCLUDE_INTERREFLECTION,
    DEFAULT_MAINTENANCE_FACTOR,
    DEFAULT_WALL_MARGIN_M,
    DEFAULT_WALL_REFLECTANCE,
    IES_FETCH_TIMEOUT_S,
    MAX_IES_UPLOAD_BYTES,
)
from ies_parser import IesParseError, parse_ies
from models import Fixture
from room_geometry import RoomShape, RoomShapeError

api = Blueprint("api", __name__, url_prefix="/api")

# id -> IesData. Backed by ies_store on disk (see that module's docstring) -
# _preload_saved() below repopulates this from disk every time the process
# starts, so a Passenger restart doesn't lose previously saved files.
_ies_store = {}


def _preload_saved() -> None:
    for entry in ies_store.list_saved():
        ies_id = entry["id"]
        raw = ies_store.load_text(ies_id)
        if raw is None:
            continue
        try:
            _ies_store[ies_id] = parse_ies(raw, source_filename=entry.get("filename", "saved.ies"))
        except IesParseError:
            # A previously-saved file that no longer parses (shouldn't
            # normally happen) - skip it rather than crash app startup.
            continue


_preload_saved()


def _get_ies_data(ies_id):
    """Look up parsed IES data by id, checking the in-process cache first
    and falling back to disk.

    This fallback matters even though every route already writes into
    _ies_store on upload/fetch: Passenger (and most WSGI hosts) commonly
    runs more than one worker process, each with its OWN copy of this
    module-level dict. An upload handled by worker A never appears in
    worker B's memory - only on shared disk, via ies_store. Every route
    that reads an ies_id must go through this function rather than
    touching _ies_store directly, or a request routed to a different
    worker than the one that received the upload/fetch will incorrectly
    report "Unknown or missing ies_id" even though the file exists.
    Returns None if the id isn't known anywhere (never uploaded, wrong
    id, or the saved file was deleted).
    """
    ies_data = _ies_store.get(ies_id)
    if ies_data is not None:
        return ies_data

    raw = ies_store.load_text(ies_id)
    meta = ies_store.load_meta(ies_id)
    if raw is None or meta is None:
        return None
    try:
        ies_data = parse_ies(raw, source_filename=meta.get("filename", "saved.ies"))
    except IesParseError:
        return None
    _ies_store[ies_id] = ies_data
    return ies_data


def _ies_metadata_payload(ies_id: str, ies_data) -> dict:
    """Shared shape for /upload, /fetch_url and /list-entry responses."""
    return {
        "id": ies_id,
        "metadata": {
            "filename": ies_data.source_filename,
            "lamp_count": ies_data.lamp_count,
            "lumens_per_lamp": ies_data.lumens_per_lamp,
            "total_lumens": ies_data.total_lumens,
            "input_watts": ies_data.input_watts,
            "ballast_factor": ies_data.ballast_factor,
            "vertical_angle_count": len(ies_data.vertical_angles),
            "horizontal_angle_count": len(ies_data.horizontal_angles),
            "max_vertical_angle": ies_data.max_vertical_angle,
            "max_horizontal_angle": ies_data.max_horizontal_angle,
            "tilt_supported": ies_data.tilt_supported,
        },
    }


@api.route("/ies/upload", methods=["POST"])
def upload_ies():
    """Accept a multipart-form .ies file upload, parse it, and return an
    id (to reference it in later calls) plus display metadata."""
    if "ies_file" not in request.files:
        return jsonify({"error": "No file field named 'ies_file' in the request."}), 400

    file = request.files["ies_file"]
    raw = file.read(MAX_IES_UPLOAD_BYTES + 1)
    if len(raw) > MAX_IES_UPLOAD_BYTES:
        return jsonify({"error": "File too large."}), 400

    filename = file.filename or "upload.ies"
    try:
        text = raw.decode("utf-8", errors="replace")
        ies_data = parse_ies(text, source_filename=filename)
    except IesParseError as exc:
        return jsonify({"error": f"Could not parse IES file: {exc}"}), 400

    ies_id = ies_store.save(text, filename=filename, source="upload")
    _ies_store[ies_id] = ies_data

    return jsonify(_ies_metadata_payload(ies_id, ies_data))


@api.route("/ies/fetch_url", methods=["POST"])
def fetch_ies_from_url():
    """Fetch a .ies file from a URL the user pastes in, parse it, save it
    the same way an upload is saved, and return the same response shape
    as /ies/upload so the frontend can treat both identically.

    Body: {"url": "https://example.com/fixture.ies"}
    """
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Request body must include a non-empty 'url'."}), 400
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"error": "Only http:// and https:// URLs are supported."}), 400

    req = urllib.request.Request(url, headers={"User-Agent": "sc-ies-shape-tool/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=IES_FETCH_TIMEOUT_S) as resp:
            raw = resp.read(MAX_IES_UPLOAD_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return jsonify({"error": f"Server returned HTTP {exc.code} for that URL."}), 400
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return jsonify({"error": f"Could not fetch that URL: {exc.reason if hasattr(exc, 'reason') else exc}"}), 400

    if len(raw) > MAX_IES_UPLOAD_BYTES:
        return jsonify({"error": "File too large."}), 400

    filename = url.rsplit("/", 1)[-1] or "fetched.ies"
    try:
        text = raw.decode("utf-8", errors="replace")
        ies_data = parse_ies(text, source_filename=filename)
    except IesParseError as exc:
        return jsonify({"error": f"Could not parse IES file: {exc}"}), 400

    ies_id = ies_store.save(text, filename=filename, source="url", source_url=url)
    _ies_store[ies_id] = ies_data

    return jsonify(_ies_metadata_payload(ies_id, ies_data))


@api.route("/ies/list", methods=["GET"])
def list_saved_ies():
    """List every previously saved IES file (upload or URL fetch) so the
    frontend can offer 'use one you've already saved' without re-uploading."""
    return jsonify({"files": ies_store.list_saved()})


@api.route("/ies/<ies_id>", methods=["GET"])
def load_saved_ies(ies_id):
    """Re-select a previously saved file by id (from /ies/list) without
    re-uploading it."""
    ies_data = _get_ies_data(ies_id)
    if ies_data is None:
        return jsonify({"error": "No saved IES file with that id."}), 404
    return jsonify(_ies_metadata_payload(ies_id, ies_data))


@api.route("/heatmap", methods=["POST"])
def calculate_heatmap():
    """Single-fixture, rectangular-room heatmap (phase 1). Body is JSON:

        {
          "ies_id": "...",
          "room_width": 10.0, "room_length": 10.0,
          "mounting_height": 3.0, "work_plane_height": 0.0,
          "fixture_x": 5.0, "fixture_y": 5.0,
          "grid_spacing": 0.5, "wall_margin": 0.5
        }

    fixture_x/y, grid_spacing and wall_margin are optional; sensible
    defaults are applied if omitted.
    """
    body = request.get_json(silent=True) or {}

    ies_id = body.get("ies_id")
    ies_data = _get_ies_data(ies_id)
    if ies_data is None:
        return jsonify({"error": "Unknown or missing ies_id. Upload an IES file first."}), 400

    try:
        room_width = float(body["room_width"])
        room_length = float(body["room_length"])
        mounting_height = float(body["mounting_height"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "room_width, room_length and mounting_height are required numbers."}), 400

    work_plane_height = float(body.get("work_plane_height", 0.0))
    fixture_x = float(body.get("fixture_x", room_width / 2))
    fixture_y = float(body.get("fixture_y", room_length / 2))
    grid_spacing = float(body.get("grid_spacing", DEFAULT_GRID_SPACING_M))
    wall_margin = float(body.get("wall_margin", DEFAULT_WALL_MARGIN_M))

    if mounting_height <= work_plane_height:
        return jsonify({"error": "mounting_height must be greater than work_plane_height."}), 400

    fixture = Fixture(x=fixture_x, y=fixture_y, mounting_height=mounting_height, ies=ies_data)

    try:
        points = grid_generator.generate_rectangular_grid(
            width=room_width, length=room_length,
            spacing=grid_spacing, wall_margin=wall_margin,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    for point in points:
        point.value = photometry.total_illuminance(
            [fixture], point.x, point.y, work_plane_z=work_plane_height
        )

    result = aggregator.summarize([p.value for p in points])
    heatmap_payload = heatmap_render.render_grid(points)

    return jsonify({
        "room": {"width": room_width, "length": room_length},
        "fixture": {"x": fixture_x, "y": fixture_y, "mounting_height": mounting_height},
        "work_plane_height": work_plane_height,
        "heatmap": heatmap_payload,
        "uniformity": {
            "e_min": result.e_min,
            "e_max": result.e_max,
            "e_avg": result.e_avg,
            "u0": result.u0,
            "u1": result.u1,
        },
    })


# ---------------------------------------------------------------------------
# PHASE 2: arbitrary-shape, multi-fixture pipeline
# ---------------------------------------------------------------------------

def _shape_from_request(body: dict) -> RoomShape:
    """Shared helper: pull the "shape" spec out of a request body and
    build a validated RoomShape, or raise a (message, http_status) pair
    via RoomShapeError so every route reports shape problems the same
    way."""
    spec = body.get("shape")
    if not spec:
        raise RoomShapeError("Request body must include a 'shape' object.")
    return RoomShape.from_spec(spec)


@api.route("/shape", methods=["POST"])
def validate_shape():
    """Validate a room shape and return its normalized description (area,
    bounds, exterior/hole vertex lists) for the frontend to draw. Body:

        {"shape": {"type": "rectangle", "width": 10, "length": 8}}
        or
        {"shape": {"type": "polygon",
                    "exterior": [[0,0], [10,0], [10,6], [4,6], [4,10], [0,10]],
                    "holes": [[[7,2],[8,2],[8,3],[7,3]]]}}
    """
    body = request.get_json(silent=True) or {}
    try:
        shape = _shape_from_request(body)
    except RoomShapeError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"shape": shape.to_dict()})


@api.route("/layout", methods=["POST"])
def generate_layout():
    """Generate a fixture lattice clipped to a shape. Body:

        {
          "shape": {...same spec as /api/shape...},
          "spacing_x": 3.0, "spacing_y": 3.0,
          "pattern": "grid" | "staggered",
          "rotation_deg": 0.0,
          "wall_margin": 0.5
        }

    Returns the actual retained fixture positions plus the actual
    achieved spacing - never just the requested spacing echoed back
    (see fixture_layout.py docstring / plan §5).
    """
    body = request.get_json(silent=True) or {}

    try:
        shape = _shape_from_request(body)
    except RoomShapeError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        spacing_x = float(body["spacing_x"])
        spacing_y = float(body["spacing_y"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "spacing_x and spacing_y are required numbers."}), 400

    pattern = body.get("pattern", "grid")
    rotation_deg = float(body.get("rotation_deg", 0.0))
    wall_margin = float(body.get("wall_margin", DEFAULT_WALL_MARGIN_M))

    try:
        layout = fixture_layout.generate_lattice(
            shape,
            spacing_x=spacing_x,
            spacing_y=spacing_y,
            pattern=pattern,
            rotation_deg=rotation_deg,
            wall_margin=wall_margin,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({
        "shape": shape.to_dict(),
        "positions": [[x, y] for x, y in layout.positions],
        "count": layout.count,
        "requested_spacing_x": layout.requested_spacing_x,
        "requested_spacing_y": layout.requested_spacing_y,
        "actual_spacing_x": layout.actual_spacing_x,
        "actual_spacing_y": layout.actual_spacing_y,
        "pattern": layout.pattern,
        "rotation_deg": layout.rotation_deg,
        "wall_margin": layout.wall_margin,
    })


@api.route("/calculate", methods=["POST"])
def calculate():
    """Full pipeline: shape + placed fixtures + IES data -> measurement
    grid -> per-point sum -> aggregate. Body:

        {
          "ies_id": "...",
          "shape": {...same spec as /api/shape...},
          "fixtures": [[x, y], [x, y], ...],
          "mounting_height": 3.0, "work_plane_height": 0.0,
          "grid_resolution": 0.5, "wall_margin": 0.5
        }

    `fixtures` is normally whatever `/api/layout` returned as
    `positions` (optionally hand-edited), or a single manually-placed
    point - this route doesn't regenerate a layout itself, so what you
    see previewed is exactly what gets calculated.
    """
    body = request.get_json(silent=True) or {}

    ies_id = body.get("ies_id")
    ies_data = _get_ies_data(ies_id)
    if ies_data is None:
        return jsonify({"error": "Unknown or missing ies_id. Upload an IES file first."}), 400

    try:
        shape = _shape_from_request(body)
    except RoomShapeError as exc:
        return jsonify({"error": str(exc)}), 400

    raw_fixtures = body.get("fixtures")
    if not raw_fixtures:
        return jsonify({"error": "'fixtures' must be a non-empty list of [x, y] positions."}), 400

    try:
        mounting_height = float(body["mounting_height"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "mounting_height is a required number."}), 400

    work_plane_height = float(body.get("work_plane_height", 0.0))
    if mounting_height <= work_plane_height:
        return jsonify({"error": "mounting_height must be greater than work_plane_height."}), 400

    # --- Declared-lumens rescale (the "DIALux lets you edit the lumen/
    # power and it rescales the candela distribution" feature). One
    # override applies to every fixture in this calc, since they all
    # share the single uploaded ies_id. Omit/null 'declared_lumens' to
    # use the file's own declared output unscaled (flux_scale = 1.0).
    file_total_lumens = photometry.total_flux(ies_data)
    declared_lumens_raw = body.get("declared_lumens")
    flux_scale = 1.0
    declared_lumens = None
    if declared_lumens_raw not in (None, ""):
        try:
            declared_lumens = float(declared_lumens_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "declared_lumens must be a number."}), 400
        if declared_lumens < 0:
            return jsonify({"error": "declared_lumens cannot be negative."}), 400
        if file_total_lumens > 0:
            flux_scale = declared_lumens / file_total_lumens
        # file_total_lumens == 0 (degenerate file) - flux_scale stays 1.0
        # rather than dividing by zero; the override is effectively a
        # no-op in that edge case, which is the safest failure mode.

    # 'declared_watts' is informational only (echoed back as an
    # efficacy figure) - it does not feed the illuminance calculation,
    # matching how DIALux's wattage field is independent of the
    # photometric scaling itself.
    declared_watts_raw = body.get("declared_watts")
    declared_watts = None
    if declared_watts_raw not in (None, ""):
        try:
            declared_watts = float(declared_watts_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "declared_watts must be a number."}), 400

    try:
        fixtures = [
            Fixture(
                x=float(pos[0]), y=float(pos[1]),
                mounting_height=mounting_height, ies=ies_data,
                flux_scale=flux_scale,
            )
            for pos in raw_fixtures
        ]
    except (TypeError, ValueError, IndexError):
        return jsonify({"error": "Each entry in 'fixtures' must be an [x, y] pair."}), 400

    grid_resolution = float(body.get("grid_resolution", DEFAULT_GRID_SPACING_M))
    wall_margin = float(body.get("wall_margin", DEFAULT_WALL_MARGIN_M))

    # --- Reflectances, ceiling height, maintenance factor, interreflection ---
    try:
        ceiling_reflectance = float(body.get("ceiling_reflectance", DEFAULT_CEILING_REFLECTANCE))
        wall_reflectance = float(body.get("wall_reflectance", DEFAULT_WALL_REFLECTANCE))
        floor_reflectance = float(body.get("floor_reflectance", DEFAULT_FLOOR_REFLECTANCE))
        maintenance_factor = float(body.get("maintenance_factor", DEFAULT_MAINTENANCE_FACTOR))
    except (TypeError, ValueError):
        return jsonify({"error": "reflectances and maintenance_factor must be numbers."}), 400

    for name, value in (
        ("ceiling_reflectance", ceiling_reflectance),
        ("wall_reflectance", wall_reflectance),
        ("floor_reflectance", floor_reflectance),
    ):
        if not (0.0 <= value <= 1.0):
            return jsonify({"error": f"{name} must be between 0 and 1 (e.g. 0.70 for 70%)."}), 400
    if maintenance_factor <= 0.0:
        return jsonify({"error": "maintenance_factor must be greater than 0."}), 400

    # Ceiling height for the wall-area estimate defaults to the fixture
    # mounting height (correct for flush/surface-mounted fixtures) -
    # override it if fixtures are pendant-mounted below the real ceiling.
    ceiling_height_raw = body.get("ceiling_height")
    ceiling_height = (
        float(ceiling_height_raw) if ceiling_height_raw not in (None, "") else mounting_height
    )

    include_interreflection = bool(body.get("include_interreflection", DEFAULT_INCLUDE_INTERREFLECTION))

    try:
        points = grid_generator.generate_measurement_grid(
            shape, resolution=grid_resolution, wall_margin=wall_margin
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Direct component has to be computed first now: v2 of the
    # interreflection model grounds itself in the grid's own measured
    # direct average rather than re-deriving an idealized flux density
    # (see interreflection.py's module docstring for why that mattered).
    started = time.perf_counter()
    direct_values = [
        photometry.total_illuminance(fixtures, point.x, point.y, work_plane_z=work_plane_height)
        for point in points
    ]

    # spatial_weight: 0 = flat wall-indirect (v2 behavior), 1 = fully
    # redistributed (validated to overshoot U0 on the reference room —
    # see interreflection.py's module docstring). Defaults to the
    # empirically-matched value; expose it as a request field if you
    # want to tune it per project rather than editing DEFAULT here.
    spatial_weight = float(body.get("interreflection_spatial_weight", 0.45))

    ceiling_component = 0.0
    wall_per_point = []
    if include_interreflection and direct_values:
        direct_avg_raw = sum(direct_values) / len(direct_values)
        components = interreflection.solve_indirect_components(
            fixtures, shape, ceiling_height=ceiling_height,
            ceiling_reflectance=ceiling_reflectance,
            wall_reflectance=wall_reflectance,
            floor_reflectance=floor_reflectance,
            direct_avg_illuminance=direct_avg_raw,
        )
        ceiling_component = components.ceiling_component
        wall_per_point = interreflection.spatial_wall_indirect(
            points, shape, ceiling_height=ceiling_height,
            components=components, spatial_weight=spatial_weight,
        )
        if not wall_per_point:
            # Degenerate case (e.g. zero wall reflectance) - flat fallback.
            wall_per_point = [components.wall_component_avg] * len(points)

    indirect_illuminance = ceiling_component + (
        sum(wall_per_point) / len(wall_per_point) if wall_per_point else 0.0
    )  # kept for the response payload's "indirect_illuminance_lux" field (room average)

    for i, (point, direct) in enumerate(zip(points, direct_values)):
        wall_term = wall_per_point[i] if wall_per_point else 0.0
        point.value = (direct + ceiling_component + wall_term) * maintenance_factor
    elapsed_s = time.perf_counter() - started

    result = aggregator.summarize([p.value for p in points])
    heatmap_payload = heatmap_render.render_grid(points)

    return jsonify({
        "shape": shape.to_dict(),
        "fixtures": [{"x": f.x, "y": f.y, "mounting_height": f.mounting_height} for f in fixtures],
        "work_plane_height": work_plane_height,
        "grid_resolution": grid_resolution,
        "wall_margin": wall_margin,
        "grid_point_count": len(points),
        "calc_time_seconds": elapsed_s,
        "heatmap": heatmap_payload,
        "photometric_output": {
            "file_total_lumens": file_total_lumens,
            "declared_lumens": declared_lumens,
            "flux_scale": flux_scale,
            "declared_watts": declared_watts,
            "efficacy_lm_per_w": (
                (declared_lumens if declared_lumens is not None else file_total_lumens) / declared_watts
                if declared_watts else None
            ),
        },
        "environment": {
            "ceiling_reflectance": ceiling_reflectance,
            "wall_reflectance": wall_reflectance,
            "floor_reflectance": floor_reflectance,
            "ceiling_height": ceiling_height,
            "maintenance_factor": maintenance_factor,
            "include_interreflection": include_interreflection,
            "indirect_illuminance_lux": indirect_illuminance,
        },
        "uniformity": {
            "e_min": result.e_min,
            "e_max": result.e_max,
            "e_avg": result.e_avg,
            "u0": result.u0,
            "u1": result.u1,
        },
    })
