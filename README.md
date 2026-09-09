# IES Heatmap & Shape Tool — Phase 2

Prototype described in `IES_HEATMAP_SHAPE_TOOL_PLAN.md`. Phase 1 proved
the two riskiest "must be correct" pieces in isolation (IES parsing,
point-by-point photometry). Phase 2 adds the part flagged in the plan as
most likely to go wrong: correct fixture duplication and measurement
across **any room shape** — rectangles, concave rooms, and rooms with
obstacles (holes) — not just a plain rectangle.

## What it does now

1. **Import an IES file** → parsed metadata shown (lamp count, lumens,
   wattage, angle coverage).
2. **Define a room shape** — a rectangle, or an arbitrary polygon (vertex
   list) with optional holes for obstacles like columns — validated via
   `shapely` (`room_geometry.py`), including automatic repair/rejection
   of self-intersecting input.
3. **Place fixtures** — either an auto-generated grid/staggered lattice
   with configurable spacing, rotation, and wall margin
   (`fixture_layout.py`), clipped to the actual shape (concave notches
   and holes both correctly exclude fixtures), or a manual list of
   positions.
4. **Calculate** — a measurement grid (independent resolution/margin from
   fixture spacing) clipped to the same shape, illuminance summed at
   every point from every fixture via the same ground-truth point-by-point
   method as phase 1, aggregated into `E_min`, `E_max`, `E_avg`, `U0`,
   `U1`, and rendered as a heatmap with the room outline, holes (dashed),
   and fixture markers drawn on top.

## Interactive canvas editing (shape + fixtures)

Room shape and fixture placement no longer require hand-typed JSON as the
primary route in (the JSON textareas are still there as a "paste" fallback
under Step 2's "Polygon" option):

- **Draw the room shape** — pick "Polygon" → "Draw on canvas" in Step 2.
  Click to place points; click the first (green) point again, or hit
  **Close Shape**, to finish the outline. Once closed: drag a white dot to
  move a corner, drag a grey diamond (an edge's midpoint handle) to bow
  that side into a curve, double-click an edge to insert a corner there,
  and double-click (or right-click) a corner to remove it. **Add Hole**
  draws an obstacle ring the same way, once the outline is closed.
  - **Exact side lengths**: every edge shows a length label (e.g.
    `3.50 m`) live as you draw. Rather than clicking approximately, aim
    the mouse in the direction you want the next wall to run, type the
    exact distance into "Next side length (m)", and hit **Add Side at
    This Length** (or press Enter) — the vertex lands exactly that far
    away in whatever direction the mouse is aiming. **Snap angle**
    (15°/45°/90°) constrains that aim direction too, so square corners
    come out exactly square even with a slightly sloppy mouse position.
  - Already-placed edges stay editable: click any edge's length label
    (any time — mid-drawing or after the shape is closed) to type a
    corrected value; the edge's far vertex moves to match while its near
    vertex stays put, so a roughly-sketched shape can be dimensioned
    precisely afterward.
  Curves are a *design-time* convenience only — `static/js/shape_editor.js`
  flattens each curve into a dense straight-edge polyline (a quadratic
  Bezier sampled at 16 segments) before it's sent to `/api/shape`, so
  `room_geometry.py` never has to know curves were involved; the wire
  format is still exactly the `{"type": "polygon", "exterior": [...],
  "holes": [...]}` spec described below.
- **Edit fixtures** — once fixtures are placed (Step 3, either mode), the
  workspace canvas becomes editable: drag a fixture to reposition it,
  switch to "Add Fixture" or "Remove Fixture" mode to click-place or
  click-delete, and **Add Row/Column** duplicates the outermost row or
  column of the current layout at a given spacing, skipping any resulting
  point that would land outside the room shape. This lives in
  `static/js/fixture_editor.js` and only ever touches the plain `[[x,
  y], ...]` fixture list already used everywhere else.
- Implemented entirely in the frontend (`shape_editor.js`,
  `fixture_editor.js`, `geometry_utils.js`, plus a refactor of
  `canvas_heatmap.js` into reusable transform/layer functions so the
  editors share the same coordinate math as the read-only render path) —
  no backend routes or `room_geometry.py`/`fixture_layout.py` changes were
  needed, since both editors only ever produce the same JSON shapes those
  modules already validate.

## What it does NOT do yet (by design — later phases)

- Fixtures are assumed to aim straight down with no rotation of their own
  (an `aim_vector`/tilt parameter is a noted extension point in the plan,
  §9).
- DXF/DWG import as a room-shape source (§9 — worth checking whether
  vertex extraction from the existing DWG-analysis tooling can be reused
  here instead of only free-hand drawing or pasted vertices).
- Point-by-point illuminance summation is `O(grid points x fixtures)`
  with no spatial indexing/distance cutoff — fine at prototype scale (see
  the performance check in `test_shape_pipeline.py`), flagged as the
  first thing to revisit if this gets folded into the main simulator for
  much larger rooms.
- `TILT=INCLUDE` / external tilt files are parsed enough to keep the rest
  of the file aligned, but the tilt multiplying factors are not applied
  (flagged in the API response as `tilt_supported: false` when relevant).

## Running it

```bash
cd ies_shape_tool
pip install -r requirements.txt
python app.py
```

Then open `http://127.0.0.1:5000`. A synthetic test fixture is included
at `sample_data/sample_floodlight.ies` if you want to try it before using
a real vendor file — it's a rotationally-symmetric profile with round
numbers, chosen specifically so its expected illuminance values can be
checked by hand (see `test_photometry.py`).

To try a non-rectangular shape without hand-typing vertices, paste this
into the "Exterior vertices" field for an L-shaped room:
`[[0,0],[10,0],[10,6],[6,6],[6,10],[0,10]]`, and this into "Holes" for a
2x2 column near the middle: `[[[4,4],[6,4],[6,6],[4,6]]]`.

## Verifying the math and geometry independently of the UI

```bash
python test_photometry.py       # phase 1: parser + point-by-point physics
python test_shape_pipeline.py   # phase 2: rectangle regression, concave shape,
                                 # hole, actual-vs-target spacing, perf check
```

`test_shape_pipeline.py` runs items 2–6 of the engineering plan's §7
applicability test plan (item 1, physics sanity, is `test_photometry.py`):
a rectangle run through the polygon-clipped path must match the naive
rectangular path exactly; an L-shaped room must exclude its missing
quadrant from both fixtures and measurement points; a room with a hole
must exclude the obstacle from both; the reported "actual" fixture
spacing must be a real recomputation (not just an echo of the requested
value) both for a non-evenly-dividing spacing and for a rotated lattice;
and a timing check reports the point-by-point cost at a moderate room
size. All currently pass.

## File map

| File | Responsibility |
|---|---|
| `app.py` | Flask app factory, serves the page. No calculation logic. |
| `config.py` | Tunable defaults (grid spacing, margins, color scale). No logic. |
| `models.py` | Shared dataclasses (`IesData`, `Fixture`, `GridPoint`, `UniformityResult`). |
| `ies_parser.py` | Raw `.ies` text → `IesData`. |
| `photometry.py` | Candela interpolation + point-by-point illuminance formula. |
| `room_geometry.py` | **Phase 2.** `RoomShape` — shapely-backed polygon (rectangle or arbitrary vertices + holes), validation/repair, erosion, containment. |
| `fixture_layout.py` | **Phase 2.** Grid/staggered fixture lattice generation, clipped to a `RoomShape`, with actual-vs-requested spacing. |
| `grid_generator.py` | `generate_rectangular_grid` (phase 1, untouched) + `generate_measurement_grid` (**phase 2**, polygon-clipped). |
| `aggregator.py` | `E_min/E_max/E_avg/U0/U1` from a value list. |
| `heatmap_render.py` | Value grid → JSON + hex colors for the frontend canvas. |
| `api_routes.py` | Flask Blueprint. Phase 1: `/api/ies/upload`, `/api/heatmap`. Phase 2: `/api/shape`, `/api/layout`, `/api/calculate`. |
| `templates/index.html`, `static/` | Single-page frontend, SC-brand styled — now a 4-step shape/fixture/calculate flow. |
| `test_photometry.py` | Phase 1 standalone sanity tests. |
| `test_shape_pipeline.py` | Phase 2 standalone sanity tests (plan §7 items 2–6). |

## API summary (phase 2 additions)

- `POST /api/shape` — `{"shape": {...}}` → validated `{area, bounds, exterior, holes}`.
- `POST /api/layout` — shape + `spacing_x`/`spacing_y`/`pattern`/`rotation_deg`/`wall_margin`
  → `{positions, count, requested_spacing_x/y, actual_spacing_x/y, ...}`.
- `POST /api/calculate` — `ies_id` + shape + `fixtures` (list of `[x,y]`) + mounting/work-plane
  heights + grid resolution/margin → `{shape, fixtures, heatmap, uniformity, grid_point_count, calc_time_seconds}`.

Shape spec format (shared by all three routes):
```json
{"type": "rectangle", "width": 10, "length": 8}
```
or
```json
{"type": "polygon",
 "exterior": [[0,0],[10,0],[10,6],[6,6],[6,10],[0,10]],
 "holes": [[[4,4],[6,4],[6,6],[4,6]]]}
```

The phase-1 `/api/heatmap` route (single fixture, rectangle only) is
untouched and still works, but the frontend now uses the phase-2 routes
for everything (a rectangle is just `{"type": "rectangle", ...}` through
the same pipeline, so nothing was lost by switching over).

## Next phase

Per the plan's build order, item 7: only after the §7 test plan passes on
rectangle, concave, and holed shapes (it does — see `test_shape_pipeline.py`)
is this ready to be evaluated for folding into the main
simulator/LuxScale codebase. Open items before that, per plan §9:
deciding on a room-shape *input* method beyond a JSON vertex list (canvas
drawing vs. DWG import), and deciding whether to stub an `aim_vector`
parameter into `photometry.point_illuminance` now to avoid a signature
change later if tilted/aimed fixtures are needed.
