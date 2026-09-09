/**
 * app.js
 * ------
 * Wires the four-step form to the API:
 *   1. Upload IES file              -> POST /api/ies/upload
 *   2. Validate room shape          -> POST /api/shape
 *   3. Place fixtures (auto/manual) -> POST /api/layout, or parsed inline
 *   4. Calculate                    -> POST /api/calculate
 *
 * Each step's result is kept in module-level state so the next step can
 * reuse it without re-deriving anything.
 *
 * INTERACTIVE EDITORS: the single workspace <canvas> is shared across
 * steps. Exactly one of ShapeEditor / FixtureEditor "owns" pointer input
 * at any moment (`activeEditor`, decided by `_recomputeActiveEditor`):
 * the shape editor owns it while the user is drawing/tweaking a polygon
 * room outline, the fixture editor owns it once fixtures exist. Every
 * mutation from either editor re-runs `render()`, which fits the canvas
 * to whichever bounds are relevant right now (the shape being drawn, the
 * validated shape, or nothing yet) and layers heatmap -> shape -> fixtures
 * -> the active editor's own overlay (handles, drag highlights, etc.).
 */

let currentIesId = null;
let currentShape = null;      // {area, bounds, exterior, holes} - from the last successful /api/shape
let currentFixtures = null;   // [[x, y], ...]
let currentHeatmap = null;    // heatmap payload from the last /api/calculate
let currentGridResolution = null; // meters/cell, echoed back by /api/calculate - drives exact heatmap cell sizing

let activeEditor = null; // 'shape' | 'fixtures' | null

const canvas = document.getElementById('heatmap-canvas');
const ctx = canvas.getContext('2d');
const luxTooltip = document.getElementById('lux-tooltip');

// -- shared workspace rendering -----------------------------------------------

function _defaultBounds() {
  return { min_x: 0, min_y: 0, max_x: 20, max_y: 20 };
}

function computeViewBounds() {
  if (currentShape) return currentShape.bounds;
  if (activeEditor === 'shape') {
    const drawn = ShapeEditor.boundingBox();
    if (drawn) return drawn;
  }
  return _defaultBounds();
}

// -- interactive camera (zoom + pan) ------------------------------------------
// Purely a view transform on top of the existing fit-to-canvas math - never
// touches world coordinates, fixture positions, or the shape itself. Scroll
// wheel zooms anchored under the cursor; the middle mouse button pans (left
// click stays reserved for the shape/fixture editors, so this never competes
// with drawing a polygon or dragging a fixture).
const CAMERA_MIN_ZOOM = 0.5;
const CAMERA_MAX_ZOOM = 10;
let camera = { zoom: 1, panX: 0, panY: 0 };

function _getTransform() {
  return computeViewTransform(canvas, computeViewBounds(), 20, camera);
}

function resetCamera() {
  camera = { zoom: 1, panX: 0, panY: 0 };
  render();
}

function zoomBy(factor, anchorScreenPt) {
  const before = _getTransform();
  const anchor = anchorScreenPt || { x: canvas.width / 2, y: canvas.height / 2 };
  const worldAtAnchor = { x: before.fromX(anchor.x), y: before.fromY(anchor.y) };

  camera.zoom = Math.min(CAMERA_MAX_ZOOM, Math.max(CAMERA_MIN_ZOOM, camera.zoom * factor));

  // Re-anchor: keep whatever world point was under the cursor/center fixed
  // on screen after the zoom, instead of zooming toward the canvas corner.
  const after = _getTransform();
  camera.panX += anchor.x - after.toX(worldAtAnchor.x);
  camera.panY += anchor.y - after.toY(worldAtAnchor.y);
  render();
}

canvas.addEventListener('wheel', (evt) => {
  evt.preventDefault();
  const screenPt = _canvasMousePos(evt);
  zoomBy(evt.deltaY < 0 ? 1.15 : 1 / 1.15, screenPt);
}, { passive: false });

let panDrag = null; // {startClientX, startClientY, startPanX, startPanY}

canvas.addEventListener('mousedown', (evt) => {
  if (evt.button === 1) { // middle mouse button = pan, regardless of active editor
    evt.preventDefault();
    panDrag = { startClientX: evt.clientX, startClientY: evt.clientY, startPanX: camera.panX, startPanY: camera.panY };
  }
});
window.addEventListener('mousemove', (evt) => {
  if (!panDrag) return;
  camera.panX = panDrag.startPanX + (evt.clientX - panDrag.startClientX);
  camera.panY = panDrag.startPanY + (evt.clientY - panDrag.startClientY);
  render();
});
window.addEventListener('mouseup', (evt) => {
  if (evt.button === 1) panDrag = null;
});

// -- Optional fixed color scale (so successive Calculate runs are visually
// comparable instead of each one re-stretching red-to-blue across its own
// min/max, which can make a genuine but small change hard to see) --------

// Mirrors config.py's HEATMAP_COLOR_STOPS exactly - keep the two in sync.
const HEATMAP_COLOR_STOPS = [
  [0.00, [0, 0, 255]],
  [0.25, [0, 255, 255]],
  [0.50, [0, 255, 0]],
  [0.75, [255, 255, 0]],
  [1.00, [255, 0, 0]],
];

function _hex(rgb) {
  return '#' + rgb.map((c) => Math.max(0, Math.min(255, Math.round(c))).toString(16).padStart(2, '0')).join('');
}

function _colorForFraction(fraction) {
  fraction = Math.min(1, Math.max(0, fraction));
  for (let i = 0; i < HEATMAP_COLOR_STOPS.length - 1; i++) {
    const [posA, colorA] = HEATMAP_COLOR_STOPS[i];
    const [posB, colorB] = HEATMAP_COLOR_STOPS[i + 1];
    if (fraction >= posA && fraction <= posB) {
      const t = posB > posA ? (fraction - posA) / (posB - posA) : 0;
      return _hex(colorA.map((c, i2) => c + (colorB[i2] - c) * t));
    }
  }
  return _hex(HEATMAP_COLOR_STOPS[HEATMAP_COLOR_STOPS.length - 1][1]);
}

const colorScaleLockCheckbox = document.getElementById('colorscale-lock-checkbox');
const colorScaleMinInput = document.getElementById('colorscale-min');
const colorScaleMaxInput = document.getElementById('colorscale-max');
const showValuesCheckbox = document.getElementById('show-values-checkbox');
const valuesDensityStatus = document.getElementById('values-density-status');
showValuesCheckbox.addEventListener('change', render);

const rulerGridCheckbox = document.getElementById('ruler-grid-checkbox');
const rulerGridSpacingInput = document.getElementById('ruler-grid-spacing');
rulerGridCheckbox.addEventListener('change', render);
rulerGridSpacingInput.addEventListener('input', render);
rulerGridSpacingInput.addEventListener('change', render);

document.getElementById('zoom-in-btn').addEventListener('click', () => zoomBy(1.25));
document.getElementById('zoom-out-btn').addEventListener('click', () => zoomBy(1 / 1.25));
document.getElementById('zoom-fit-btn').addEventListener('click', () => resetCamera());

/** The heatmap payload actually handed to the renderer: as-is (backend's
 * own per-run auto-scaled colors) unless "Lock color scale" is on and a
 * valid min/max is set, in which case every point's color is recomputed
 * here against that fixed range - same lux value always -> same color,
 * across as many Calculate runs as you like. Never touches `value`
 * (the real computed lux) or triggers a recalculation - purely a
 * re-coloring of already-computed numbers. */
function _heatmapForRender() {
  if (!currentHeatmap) return currentHeatmap;
  if (!colorScaleLockCheckbox.checked) return currentHeatmap;

  const vmin = parseFloat(colorScaleMinInput.value);
  const vmax = parseFloat(colorScaleMaxInput.value);
  if (!isFinite(vmin) || !isFinite(vmax) || vmax <= vmin) return currentHeatmap;

  return {
    ...currentHeatmap,
    points: currentHeatmap.points.map((p) => ({
      ...p,
      color: _colorForFraction((p.value - vmin) / (vmax - vmin)),
    })),
  };
}

[colorScaleLockCheckbox, colorScaleMinInput, colorScaleMaxInput].forEach((el) => {
  el.addEventListener('input', render);
  el.addEventListener('change', render);
});

// Cache of the expensive heatmap raster (thousands of fillRect calls on a
// fine grid) so pure interaction — dragging a fixture, hovering, toggling
// an unrelated checkbox — doesn't repaint it on every single frame. Only
// rebuilt when something that actually changes its pixels changes: the
// data itself, the color-scale settings, the value-label toggle, the grid
// resolution/shape, the canvas size, or the current camera (zoom/pan).
// Keyed on small primitives/version counters only (never the heatmap or
// shape object itself) - JSON-stringifying thousands of grid points on
// every render would defeat the whole point of caching them.
let _heatmapVersion = 0;
let _shapeVersion = 0;
const _heatmapRasterCache = { key: null, canvas: null, labelResult: { drawn: false, reason: null } };

function _cachedHeatmapRaster(transform) {
  const heatmapForRender = _heatmapForRender();
  const key = [
    _heatmapVersion,
    _shapeVersion,
    colorScaleLockCheckbox.checked,
    colorScaleMinInput.value,
    colorScaleMaxInput.value,
    showValuesCheckbox.checked,
    currentGridResolution,
    canvas.width, canvas.height,
    transform.scale, transform.toX(0), transform.toY(0),
  ].join('|');

  if (_heatmapRasterCache.key === key) return _heatmapRasterCache;

  const off = document.createElement('canvas');
  off.width = canvas.width;
  off.height = canvas.height;
  const offCtx = off.getContext('2d');

  drawHeatmapLayer(offCtx, transform, heatmapForRender, currentGridResolution, currentShape);
  let labelResult = { drawn: false, reason: null };
  if (showValuesCheckbox.checked && heatmapForRender) {
    labelResult = drawHeatmapValueLabels(offCtx, transform, heatmapForRender, currentGridResolution);
  }

  _heatmapRasterCache.key = key;
  _heatmapRasterCache.canvas = off;
  _heatmapRasterCache.labelResult = labelResult;
  return _heatmapRasterCache;
}

function render() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const transform = _getTransform();

  drawReferenceGrid(ctx, canvas, transform, 1);

  const raster = _cachedHeatmapRaster(transform);
  ctx.drawImage(raster.canvas, 0, 0);

  if (showValuesCheckbox.checked && currentHeatmap) {
    if (!raster.labelResult.drawn && raster.labelResult.reason === 'too-dense') {
      valuesDensityStatus.textContent =
        'Grid is too fine to print every value legibly at this zoom — zoom in, or hover a point on the heatmap for its exact reading.';
      valuesDensityStatus.className = 'sc-status';
    } else {
      valuesDensityStatus.textContent = '';
    }
  } else {
    valuesDensityStatus.textContent = '';
  }

  if (activeEditor === 'shape') {
    ShapeEditor.render(ctx, transform);
  } else if (currentShape) {
    drawShapeOutline(ctx, transform, currentShape);
  }

  if (activeEditor === 'fixtures') {
    FixtureEditor.render(ctx, transform);
  } else if (currentFixtures) {
    drawFixtureMarkers(ctx, transform, currentFixtures);
  }

  if (rulerGridCheckbox.checked) {
    const spacing = parseFloat(rulerGridSpacingInput.value);
    drawRulerOverlay(ctx, canvas, transform, transform.bounds, spacing);
  }

  return transform;
}

function _recomputeActiveEditor() {
  if (currentFixtures && currentFixtures.length) {
    activeEditor = 'fixtures';
  } else if (getShapeType() === 'polygon' && getPolygonInputMethod() === 'draw') {
    activeEditor = 'shape';
  } else {
    activeEditor = null;
  }
}

// -- shared canvas pointer plumbing -------------------------------------------

function _canvasMousePos(evt) {
  const rect = canvas.getBoundingClientRect();
  // The canvas's CSS size can differ from its pixel size (max-width:100%
  // in style.css) - scale client coords into canvas pixel space.
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  return {
    x: (evt.clientX - rect.left) * scaleX,
    y: (evt.clientY - rect.top) * scaleY,
  };
}

canvas.addEventListener('mousedown', (evt) => {
  if (evt.button !== 0) return; // middle/right button handled separately (pan / context menu)
  const screenPt = _canvasMousePos(evt);
  const transform = _getTransform();
  const worldPt = { x: transform.fromX(screenPt.x), y: transform.fromY(screenPt.y) };

  if (activeEditor === 'shape') {
    // An edge-label click opens the floating exact-length input instead of
    // the usual add-point/drag-vertex handling.
    const labelHit = ShapeEditor.hitTestEdgeLabel(screenPt, transform);
    if (labelHit) {
      _openEdgeLengthEditor(labelHit);
      return;
    }
    ShapeEditor.pointerDown(worldPt, screenPt, transform);
  } else if (activeEditor === 'fixtures') {
    FixtureEditor.pointerDown(worldPt, screenPt, transform);
  }
  render();
});

canvas.addEventListener('mousemove', (evt) => {
  const screenPt = _canvasMousePos(evt);
  const transform = _getTransform();
  const worldPt = { x: transform.fromX(screenPt.x), y: transform.fromY(screenPt.y) };
  if (activeEditor === 'shape') ShapeEditor.pointerMove(worldPt, screenPt, transform);
  else if (activeEditor === 'fixtures') FixtureEditor.pointerMove(worldPt, screenPt, transform);
  _updateLuxTooltip(evt, screenPt, transform);
  render();
});

canvas.addEventListener('mouseup', () => {
  if (activeEditor === 'shape') ShapeEditor.pointerUp();
  else if (activeEditor === 'fixtures') FixtureEditor.pointerUp();
  render();
});

canvas.addEventListener('mouseleave', () => {
  if (activeEditor === 'shape') ShapeEditor.pointerUp();
  else if (activeEditor === 'fixtures') FixtureEditor.pointerUp();
  luxTooltip.style.display = 'none';
});

/** Exact lux reading + world coordinates for whatever grid point is under
 * the cursor - the precise-value complement to the on-canvas number
 * overlay, which bails out on grids too fine for text to fit. Works
 * regardless of which editor (if any) currently owns pointer input. */
function _updateLuxTooltip(evt, screenPt, transform) {
  if (!currentHeatmap) {
    luxTooltip.style.display = 'none';
    return;
  }

  const point = findNearestHeatmapPointIndexed(transform, currentHeatmap, screenPt.x, screenPt.y, currentGridResolution);
  if (!point) {
    luxTooltip.style.display = 'none';
    return;
  }

  const rect = canvas.getBoundingClientRect();
  const wrapRect = canvasWrap.getBoundingClientRect();
  const cssScaleX = rect.width / canvas.width;
  const cssScaleY = rect.height / canvas.height;
  const left = (rect.left - wrapRect.left) + screenPt.x * cssScaleX;
  const top = (rect.top - wrapRect.top) + screenPt.y * cssScaleY;

  luxTooltip.style.left = `${left}px`;
  luxTooltip.style.top = `${top}px`;
  luxTooltip.innerHTML = `<strong>${point.value.toFixed(1)} lux</strong> &middot; (${point.x.toFixed(2)}, ${point.y.toFixed(2)}) m`;
  luxTooltip.style.display = 'block';
}

canvas.addEventListener('dblclick', (evt) => {
  if (activeEditor !== 'shape') return;
  const screenPt = _canvasMousePos(evt);
  const transform = _getTransform();
  ShapeEditor.doubleClick(screenPt, transform);
  render();
});

canvas.addEventListener('contextmenu', (evt) => {
  if (activeEditor !== 'shape') return;
  evt.preventDefault();
  const screenPt = _canvasMousePos(evt);
  const transform = _getTransform();
  ShapeEditor.rightClickDelete(screenPt, transform);
  render();
});

// -- Step 1: IES upload ------------------------------------------------------

const iesInput = document.getElementById('ies-file-input');
const iesStatus = document.getElementById('ies-status');
const iesMetadata = document.getElementById('ies-metadata');
const iesUrlInput = document.getElementById('ies-url-input');
const iesUrlFetchBtn = document.getElementById('ies-url-fetch-btn');
const iesSavedSelect = document.getElementById('ies-saved-select');

// Shared by upload / URL-fetch / load-saved, all of which return the same
// {id, metadata} shape from the backend (see api_routes._ies_metadata_payload).
function applyIesResult(data) {
  currentIesId = data.id;
  iesStatus.textContent = `Loaded: ${data.metadata.filename}`;
  iesStatus.className = 'sc-status ok';
  iesMetadata.innerHTML = renderIesMetadata(data.metadata);
  updateCalculateEnabled();
}

async function refreshSavedIesList(selectId) {
  try {
    const res = await fetch('api/ies/list');
    if (!res.ok) return;
    const data = await res.json();
    iesSavedSelect.innerHTML = '<option value="">— select a saved file —</option>';
    for (const entry of data.files) {
      const opt = document.createElement('option');
      opt.value = entry.id;
      const when = entry.saved_at ? new Date(entry.saved_at * 1000).toLocaleDateString() : '';
      opt.textContent = `${entry.filename}${when ? ' (' + when + ')' : ''}`;
      iesSavedSelect.appendChild(opt);
    }
    if (selectId) iesSavedSelect.value = selectId;
  } catch (err) {
    // Non-critical - the dropdown just stays empty/stale if this fails.
  }
}

iesInput.addEventListener('change', async () => {
  const file = iesInput.files[0];
  if (!file) return;

  iesStatus.textContent = 'Uploading & parsing...';
  iesStatus.className = 'sc-status';

  const formData = new FormData();
  formData.append('ies_file', file);

  try {
    const res = await fetch('api/ies/upload', { method: 'POST', body: formData });
    const data = await res.json();

    if (!res.ok) {
      iesStatus.textContent = data.error || 'Upload failed.';
      iesStatus.className = 'sc-status error';
      return;
    }

    applyIesResult(data);
    refreshSavedIesList(data.id);
  } catch (err) {
    iesStatus.textContent = 'Network error while uploading.';
    iesStatus.className = 'sc-status error';
  }
});

iesUrlFetchBtn.addEventListener('click', async () => {
  const url = iesUrlInput.value.trim();
  if (!url) {
    iesStatus.textContent = 'Paste a URL first.';
    iesStatus.className = 'sc-status error';
    return;
  }

  iesStatus.textContent = 'Fetching & parsing...';
  iesStatus.className = 'sc-status';

  try {
    const res = await fetch('api/ies/fetch_url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();

    if (!res.ok) {
      iesStatus.textContent = data.error || 'Fetch failed.';
      iesStatus.className = 'sc-status error';
      return;
    }

    applyIesResult(data);
    refreshSavedIesList(data.id);
  } catch (err) {
    iesStatus.textContent = 'Network error while fetching that URL.';
    iesStatus.className = 'sc-status error';
  }
});

iesSavedSelect.addEventListener('change', async () => {
  const id = iesSavedSelect.value;
  if (!id) return;

  iesStatus.textContent = 'Loading saved file...';
  iesStatus.className = 'sc-status';

  try {
    const res = await fetch(`api/ies/${id}`);
    const data = await res.json();

    if (!res.ok) {
      iesStatus.textContent = data.error || 'Could not load that saved file.';
      iesStatus.className = 'sc-status error';
      return;
    }

    applyIesResult(data);
  } catch (err) {
    iesStatus.textContent = 'Network error while loading the saved file.';
    iesStatus.className = 'sc-status error';
  }
});

// Populate the "previously saved" dropdown on page load, so files saved in
// an earlier session (or before a Passenger restart) are there right away.
refreshSavedIesList();

function renderIesMetadata(meta) {
  const totalLumens = meta.total_lumens !== null ? `${meta.total_lumens.toFixed(0)} lm` : 'n/a (absolute photometry)';
  const tiltNote = meta.tilt_supported ? '' :
    '<div class="sc-status error">Note: this file uses TILT=INCLUDE or an external tilt file — tilt correction is not applied.</div>';
  return `
    <div class="sc-status">
      Lamps: ${meta.lamp_count} &middot; Lumens/lamp: ${meta.lumens_per_lamp} &middot; Total: ${totalLumens} &middot;
      Input watts: ${meta.input_watts} &middot;
      Angles: ${meta.vertical_angle_count}v &times; ${meta.horizontal_angle_count}h
      (max ${meta.max_vertical_angle}&deg; / ${meta.max_horizontal_angle}&deg;)
    </div>
    ${tiltNote}
  `;
}

// -- Step 2: room shape -------------------------------------------------------

const shapeTypeRadios = document.querySelectorAll('input[name="shape-type"]');
const rectangleFields = document.getElementById('shape-rectangle-fields');
const polygonFields = document.getElementById('shape-polygon-fields');
const polygonMethodRadios = document.querySelectorAll('input[name="polygon-input-method"]');
const shapeDrawFields = document.getElementById('shape-draw-fields');
const shapePasteFields = document.getElementById('shape-paste-fields');
const validateShapeBtn = document.getElementById('validate-shape-btn');
const shapeStatus = document.getElementById('shape-status');
const shapeInfo = document.getElementById('shape-info');

// -- Standard-shape ("preset") fields: regular polygons, trapezoid, circle, oval --

const presetFields = document.getElementById('shape-preset-fields');
const presetShapeSelect = document.getElementById('preset-shape-select');
const presetPolygonFields = document.getElementById('preset-polygon-fields');
const presetTrapezoidFields = document.getElementById('preset-trapezoid-fields');
const presetCircleFields = document.getElementById('preset-circle-fields');
const presetOvalFields = document.getElementById('preset-oval-fields');
const presetSidesInput = document.getElementById('preset-sides');

// Named regular polygons are just `regular_polygon` with a fixed side
// count - "custom_polygon" is the only kind where the user picks `sides`
// themselves, so the sides field is read-only for every other option.
const PRESET_NAMED_SIDES = { triangle: 3, square: 4, pentagon: 5, hexagon: 6, heptagon: 7, octagon: 8 };
const PRESET_POLYGON_KINDS = new Set([...Object.keys(PRESET_NAMED_SIDES), 'custom_polygon']);

function _updatePresetFieldVisibility() {
  const kind = presetShapeSelect.value;
  const isPolygon = PRESET_POLYGON_KINDS.has(kind);
  presetPolygonFields.style.display = isPolygon ? '' : 'none';
  presetTrapezoidFields.style.display = kind === 'trapezoid' ? '' : 'none';
  presetCircleFields.style.display = kind === 'circle' ? '' : 'none';
  presetOvalFields.style.display = kind === 'oval' ? '' : 'none';

  if (isPolygon) {
    if (kind === 'custom_polygon') {
      presetSidesInput.readOnly = false;
    } else {
      presetSidesInput.value = PRESET_NAMED_SIDES[kind];
      presetSidesInput.readOnly = true;
    }
  }
}

presetShapeSelect.addEventListener('change', () => {
  _updatePresetFieldVisibility();
  _invalidateShapeAndFixtures();
  render();
});
_updatePresetFieldVisibility();

function buildPresetShapeSpec() {
  const kind = presetShapeSelect.value;

  if (kind === 'trapezoid') {
    return {
      type: 'trapezoid',
      bottom_width: parseFloat(document.getElementById('preset-trap-bottom').value),
      top_width: parseFloat(document.getElementById('preset-trap-top').value),
      height: parseFloat(document.getElementById('preset-trap-height').value),
    };
  }
  if (kind === 'circle') {
    return {
      type: 'circle',
      radius: parseFloat(document.getElementById('preset-circle-radius').value),
      segments: parseInt(document.getElementById('preset-circle-segments').value, 10),
    };
  }
  if (kind === 'oval') {
    return {
      type: 'oval',
      radius_x: parseFloat(document.getElementById('preset-oval-rx').value),
      radius_y: parseFloat(document.getElementById('preset-oval-ry').value),
      rotation_deg: parseFloat(document.getElementById('preset-oval-rotation').value || '0'),
      segments: parseInt(document.getElementById('preset-oval-segments').value, 10),
    };
  }
  // Named or custom regular polygon.
  const spec = {
    type: 'regular_polygon',
    sides: parseInt(presetSidesInput.value, 10),
    rotation_deg: parseFloat(document.getElementById('preset-rotation').value || '0'),
  };
  const dimValue = parseFloat(document.getElementById('preset-dim-value').value);
  if (document.getElementById('preset-dim-mode').value === 'side_length') {
    spec.side_length = dimValue;
  } else {
    spec.radius = dimValue;
  }
  return spec;
}

function getShapeType() {
  return document.querySelector('input[name="shape-type"]:checked').value;
}

function getPolygonInputMethod() {
  const el = document.querySelector('input[name="polygon-input-method"]:checked');
  return el ? el.value : 'draw';
}

function _invalidateShapeAndFixtures() {
  currentShape = null;
  _shapeVersion++;
  currentFixtures = null;
  currentHeatmap = null;
  _heatmapVersion++;
  generateFixturesBtn.disabled = true;
  document.getElementById('fixture-edit-toolbar').style.display = 'none';
  updateCalculateEnabled();
}

shapeTypeRadios.forEach((radio) => {
  radio.addEventListener('change', () => {
    const type = getShapeType();
    rectangleFields.style.display = type === 'rectangle' ? '' : 'none';
    polygonFields.style.display = type === 'polygon' ? '' : 'none';
    presetFields.style.display = type === 'preset' ? '' : 'none';
    _invalidateShapeAndFixtures();
    _recomputeActiveEditor();
    render();
  });
});

polygonMethodRadios.forEach((radio) => {
  radio.addEventListener('change', () => {
    const isDraw = getPolygonInputMethod() === 'draw';
    shapeDrawFields.style.display = isDraw ? '' : 'none';
    shapePasteFields.style.display = isDraw ? 'none' : '';
    _invalidateShapeAndFixtures();
    _recomputeActiveEditor();
    render();
  });
});

function buildShapeSpec() {
  const shapeType = getShapeType();
  if (shapeType === 'rectangle') {
    return {
      type: 'rectangle',
      width: parseFloat(document.getElementById('rect-width').value),
      length: parseFloat(document.getElementById('rect-length').value),
    };
  }
  if (shapeType === 'preset') {
    return buildPresetShapeSpec();
  }
  if (getPolygonInputMethod() === 'draw') {
    return ShapeEditor.toShapeSpec(); // throws a helpful Error if not finished yet
  }
  let exterior, holes;
  try {
    exterior = JSON.parse(document.getElementById('polygon-exterior').value);
    holes = JSON.parse(document.getElementById('polygon-holes').value || '[]');
  } catch (err) {
    throw new Error('Exterior/holes must be valid JSON, e.g. [[0,0],[10,0],[10,10]].');
  }
  return { type: 'polygon', exterior, holes };
}

validateShapeBtn.addEventListener('click', async () => {
  shapeStatus.textContent = 'Validating...';
  shapeStatus.className = 'sc-status';
  shapeInfo.innerHTML = '';

  let shapeSpec;
  try {
    shapeSpec = buildShapeSpec();
  } catch (err) {
    shapeStatus.textContent = err.message;
    shapeStatus.className = 'sc-status error';
    return;
  }

  try {
    const res = await fetch('api/shape', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ shape: shapeSpec }),
    });
    const data = await res.json();

    if (!res.ok) {
      shapeStatus.textContent = data.error || 'Shape validation failed.';
      shapeStatus.className = 'sc-status error';
      return;
    }

    currentShape = data.shape;
    _shapeVersion++;
    currentFixtures = null; // shape changed - previous fixture placement no longer applies
    currentHeatmap = null;
    _heatmapVersion++;
    document.getElementById('fixture-edit-toolbar').style.display = 'none';
    shapeStatus.textContent = 'Shape is valid.';
    shapeStatus.className = 'sc-status ok';
    shapeInfo.innerHTML = `<div class="sc-status">Area: ${data.shape.area.toFixed(2)} m&sup2;</div>`;

    generateFixturesBtn.disabled = false;
    updateCalculateEnabled();
    _recomputeActiveEditor();
    render();
  } catch (err) {
    shapeStatus.textContent = 'Network error while validating shape.';
    shapeStatus.className = 'sc-status error';
  }
});

// -- Step 2b: draw-on-canvas toolbar ------------------------------------------

const drawStatus = document.getElementById('draw-status');
const drawHoleBtn = document.getElementById('draw-hole-btn');

ShapeEditor.setOnChange(() => {
  drawHoleBtn.disabled = !ShapeEditor.hasClosedExterior();
  _refreshHistoryButtons();
  // Any edit to the drawn shape invalidates a previously-validated one -
  // the user must hit "Validate Shape" again before calculating.
  if (currentShape) _invalidateShapeAndFixtures();
  render();
});

document.getElementById('draw-start-btn').addEventListener('click', () => {
  ShapeEditor.startExterior();
  _recomputeActiveEditor();
  drawStatus.textContent = 'Click to place points; click the first point again to close the shape.';
  drawStatus.className = 'sc-status';
  render();
});

document.getElementById('draw-undo-btn').addEventListener('click', () => {
  ShapeEditor.undoLastPoint();
  render();
});

// -- history undo/redo (Ctrl+Z / Ctrl+Y) and other drawing shortcuts ----------
// Distinct from "Undo Point" above, which only pops the last placed vertex
// while actively drawing - this steps back through every committed change
// to the shape (drags, deletes, edge-length edits, scale, etc.).

const historyUndoBtn = document.getElementById('draw-history-undo-btn');
const historyRedoBtn = document.getElementById('draw-history-redo-btn');

function _refreshHistoryButtons() {
  historyUndoBtn.disabled = !ShapeEditor.canUndo();
  historyRedoBtn.disabled = !ShapeEditor.canRedo();
}

historyUndoBtn.addEventListener('click', () => { ShapeEditor.undo(); render(); });
historyRedoBtn.addEventListener('click', () => { ShapeEditor.redo(); render(); });

/** True while the user is typing into a text field somewhere on the page -
 * shape-editor keyboard shortcuts (undo, delete, escape, arrow-nudge) must
 * stay out of the way of ordinary form typing (e.g. arrow keys inside a
 * number input, or Escape while editing an edge length). */
function _isTypingInField(evt) {
  const tag = (evt.target.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select';
}

const NUDGE_STEP = 0.05;
const NUDGE_STEP_SHIFT = 0.5;

window.addEventListener('keydown', (evt) => {
  if (activeEditor !== 'shape') return;
  // Leave a focused field (edge-length input, next-length input, etc.) to
  // its own native/local key handling - including its own text-undo - so
  // these shortcuts only apply while the canvas itself has the user's
  // attention.
  if (_isTypingInField(evt)) return;

  const ctrlOrCmd = evt.ctrlKey || evt.metaKey;
  if (ctrlOrCmd && evt.key.toLowerCase() === 'z') {
    evt.preventDefault();
    if (evt.shiftKey) ShapeEditor.redo(); else ShapeEditor.undo();
    render();
    return;
  }
  if (ctrlOrCmd && evt.key.toLowerCase() === 'y') {
    evt.preventDefault();
    ShapeEditor.redo();
    render();
    return;
  }

  if (evt.key === 'Delete' || evt.key === 'Backspace') {
    if (!ShapeEditor.hasSelectedVertex()) return;
    evt.preventDefault();
    const result = ShapeEditor.deleteSelectedVertex();
    if (!result.ok) {
      drawStatus.textContent = result.reason;
      drawStatus.className = 'sc-status error';
    }
    render();
    return;
  }

  if (evt.key === 'Escape') {
    if (ShapeEditor.cancelActiveRing()) {
      drawStatus.textContent = 'Cancelled — click Start Drawing to begin again.';
      drawStatus.className = 'sc-status';
      render();
    }
    return;
  }

  if (evt.key.startsWith('Arrow')) {
    if (!ShapeEditor.hasSelectedVertex()) return;
    const step = evt.shiftKey ? NUDGE_STEP_SHIFT : NUDGE_STEP;
    const delta = { ArrowUp: [0, -step], ArrowDown: [0, step], ArrowLeft: [-step, 0], ArrowRight: [step, 0] }[evt.key];
    if (!delta) return;
    evt.preventDefault();
    if (ShapeEditor.nudgeSelectedVertex(delta[0], delta[1])) render();
  }
});

document.getElementById('draw-close-btn').addEventListener('click', () => {
  ShapeEditor.finishCurrentRing();
  if (ShapeEditor.hasClosedExterior()) {
    drawStatus.textContent = 'Shape closed — drag to fine-tune, or click Validate Shape.';
    drawStatus.className = 'sc-status ok';
  }
  render();
});

document.getElementById('draw-hole-btn').addEventListener('click', () => {
  ShapeEditor.startHole();
  drawStatus.textContent = 'Drawing a hole (obstacle) — click to place points, then close it the same way.';
  drawStatus.className = 'sc-status';
  render();
});

document.getElementById('draw-clear-btn').addEventListener('click', () => {
  ShapeEditor.clearAll();
  drawStatus.textContent = '';
  render();
});

const snapCheckbox = document.getElementById('draw-snap-checkbox');
const snapSizeInput = document.getElementById('draw-snap-size');
function _syncSnap() {
  ShapeEditor.setSnap(snapCheckbox.checked, parseFloat(snapSizeInput.value) || 0.25);
}
snapCheckbox.addEventListener('change', _syncSnap);
snapSizeInput.addEventListener('change', _syncSnap);

const angleSnapCheckbox = document.getElementById('draw-angle-snap-checkbox');
const angleSnapDegSelect = document.getElementById('draw-angle-snap-deg');
function _syncAngleSnap() {
  ShapeEditor.setAngleSnap(angleSnapCheckbox.checked, parseFloat(angleSnapDegSelect.value));
}
angleSnapCheckbox.addEventListener('change', _syncAngleSnap);
angleSnapDegSelect.addEventListener('change', _syncAngleSnap);

// -- exact side length: type-a-length-while-drawing, and click-a-label-to-fix --

const nextLengthInput = document.getElementById('draw-next-length');
const setLengthBtn = document.getElementById('draw-set-length-btn');

function _commitNextLength() {
  const length = parseFloat(nextLengthInput.value);
  const result = ShapeEditor.setNextSegmentLength(length);
  if (!result.ok) {
    drawStatus.textContent = result.reason;
    drawStatus.className = 'sc-status error';
    return;
  }
  drawStatus.textContent = `Placed a side ${length.toFixed(2)} m long. Aim the mouse for the next one.`;
  drawStatus.className = 'sc-status ok';
  nextLengthInput.value = '';
  nextLengthInput.focus();
  render();
}
setLengthBtn.addEventListener('click', _commitNextLength);
nextLengthInput.addEventListener('keydown', (evt) => {
  if (evt.key === 'Enter') _commitNextLength();
});

const canvasWrap = document.getElementById('workspace-canvas-wrap');
const edgeLengthEditInput = document.getElementById('edge-length-edit-input');
let editingEdge = null; // {ring, index}

function _openEdgeLengthEditor(hit) {
  editingEdge = { ring: hit.ring, index: hit.index };
  const rect = canvas.getBoundingClientRect();
  const wrapRect = canvasWrap.getBoundingClientRect();
  const cssScaleX = rect.width / canvas.width;
  const cssScaleY = rect.height / canvas.height;
  // hit.screenPos is in canvas-pixel space; position the overlay input in
  // the wrapper's own CSS pixel space (accounts for max-width scaling).
  const left = (rect.left - wrapRect.left) + hit.screenPos.x * cssScaleX;
  const top = (rect.top - wrapRect.top) + hit.screenPos.y * cssScaleY;
  edgeLengthEditInput.style.left = `${left}px`;
  edgeLengthEditInput.style.top = `${top}px`;
  edgeLengthEditInput.value = hit.length.toFixed(2);
  edgeLengthEditInput.style.display = '';
  edgeLengthEditInput.focus();
  edgeLengthEditInput.select();
}

function _closeEdgeLengthEditor() {
  edgeLengthEditInput.style.display = 'none';
  editingEdge = null;
}

function _commitEdgeLengthEditor() {
  if (!editingEdge) return;
  const length = parseFloat(edgeLengthEditInput.value);
  const result = ShapeEditor.setEdgeLength(editingEdge.ring, editingEdge.index, length);
  _closeEdgeLengthEditor();
  if (!result.ok) {
    drawStatus.textContent = result.reason;
    drawStatus.className = 'sc-status error';
  }
  render();
}

edgeLengthEditInput.addEventListener('keydown', (evt) => {
  if (evt.key === 'Enter') _commitEdgeLengthEditor();
  else if (evt.key === 'Escape') { _closeEdgeLengthEditor(); render(); }
});
edgeLengthEditInput.addEventListener('blur', _commitEdgeLengthEditor);

// -- Step 3: fixtures ---------------------------------------------------------

const fixtureModeRadios = document.querySelectorAll('input[name="fixture-mode"]');
const latticeFields = document.getElementById('fixture-lattice-fields');
const manualFields = document.getElementById('fixture-manual-fields');
const generateFixturesBtn = document.getElementById('generate-fixtures-btn');
const fixturesStatus = document.getElementById('fixtures-status');
const fixtureEditToolbar = document.getElementById('fixture-edit-toolbar');
const fixtureEditStatus = document.getElementById('fixture-edit-status');

fixtureModeRadios.forEach((radio) => {
  radio.addEventListener('change', () => {
    const isLattice = getFixtureMode() === 'lattice';
    latticeFields.style.display = isLattice ? '' : 'none';
    manualFields.style.display = isLattice ? 'none' : '';
  });
});

function getFixtureMode() {
  return document.querySelector('input[name="fixture-mode"]:checked').value;
}

function _activateFixtureEditing() {
  FixtureEditor.setFixtures(currentFixtures);
  FixtureEditor.setShapeForClipping(currentShape);
  fixtureEditToolbar.style.display = '';
  fixtureEditStatus.textContent = '';
  _recomputeActiveEditor();
}

generateFixturesBtn.addEventListener('click', async () => {
  if (!currentShape) return;

  fixturesStatus.textContent = 'Placing fixtures...';
  fixturesStatus.className = 'sc-status';

  if (getFixtureMode() === 'manual') {
    let positions;
    try {
      positions = JSON.parse(document.getElementById('manual-fixtures').value);
      if (!Array.isArray(positions) || positions.length === 0) throw new Error();
    } catch (err) {
      fixturesStatus.textContent = 'Manual fixtures must be a non-empty JSON list of [x, y] pairs.';
      fixturesStatus.className = 'sc-status error';
      return;
    }
    currentFixtures = positions;
    fixturesStatus.textContent = `${positions.length} fixture(s) placed manually.`;
    fixturesStatus.className = 'sc-status ok';
    updateCalculateEnabled();
    _activateFixtureEditing();
    render();
    return;
  }

  const shapeSpec = buildShapeSpec();
  const body = {
    shape: shapeSpec,
    spacing_x: parseFloat(document.getElementById('spacing-x').value),
    spacing_y: parseFloat(document.getElementById('spacing-y').value),
    pattern: document.getElementById('layout-pattern').value,
    rotation_deg: parseFloat(document.getElementById('rotation-deg').value),
    wall_margin: parseFloat(document.getElementById('placement-margin').value),
  };

  try {
    const res = await fetch('api/layout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();

    if (!res.ok) {
      fixturesStatus.textContent = data.error || 'Layout generation failed.';
      fixturesStatus.className = 'sc-status error';
      return;
    }

    currentFixtures = data.positions;
    fixturesStatus.textContent =
      `${data.count} fixture(s) placed — actual spacing ${data.actual_spacing_x.toFixed(2)} x ` +
      `${data.actual_spacing_y.toFixed(2)} m (requested ${data.requested_spacing_x} x ${data.requested_spacing_y} m).`;
    fixturesStatus.className = 'sc-status ok';
    updateCalculateEnabled();

    // Pre-fill the row/column editing spacing from what was just used, so
    // "Add Row" defaults to matching the lattice instead of a stale 3m.
    document.getElementById('edit-row-spacing').value = document.getElementById('spacing-y').value;
    document.getElementById('edit-col-spacing').value = document.getElementById('spacing-x').value;

    _activateFixtureEditing();
    render();
  } catch (err) {
    fixturesStatus.textContent = 'Network error while generating layout.';
    fixturesStatus.className = 'sc-status error';
  }
});

// -- Step 3b: fixture editing toolbar ------------------------------------------

FixtureEditor.setOnChange((fixtures) => {
  currentFixtures = fixtures;
  if (currentHeatmap) { currentHeatmap = null; _heatmapVersion++; } // stale once fixtures move
  fixturesStatus.textContent = `${fixtures.length} fixture(s) (edited).`;
  fixturesStatus.className = 'sc-status ok';
  updateCalculateEnabled();
  render();
});

function _setFixtureModeButton(mode) {
  FixtureEditor.setMode(mode);
  ['move', 'add', 'remove'].forEach((m) => {
    document.getElementById(`fixture-mode-${m}-btn`).classList.toggle('sc-mode-active', m === mode);
  });
  render();
}
document.getElementById('fixture-mode-move-btn').addEventListener('click', () => _setFixtureModeButton('move'));
document.getElementById('fixture-mode-add-btn').addEventListener('click', () => _setFixtureModeButton('add'));
document.getElementById('fixture-mode-remove-btn').addEventListener('click', () => _setFixtureModeButton('remove'));

function _reportEditResult(result) {
  if (result.reason) {
    fixtureEditStatus.textContent = result.reason;
    fixtureEditStatus.className = 'sc-status error';
    return;
  }
  fixtureEditStatus.textContent = result.skipped
    ? `Added ${result.added}, skipped ${result.skipped} (outside the room shape).`
    : `Added ${result.added}.`;
  fixtureEditStatus.className = 'sc-status ok';
}

document.getElementById('add-row-above-btn').addEventListener('click', () => {
  const spacing = parseFloat(document.getElementById('edit-row-spacing').value);
  _reportEditResult(FixtureEditor.addRow('above', spacing));
});
document.getElementById('add-row-below-btn').addEventListener('click', () => {
  const spacing = parseFloat(document.getElementById('edit-row-spacing').value);
  _reportEditResult(FixtureEditor.addRow('below', spacing));
});
document.getElementById('add-col-left-btn').addEventListener('click', () => {
  const spacing = parseFloat(document.getElementById('edit-col-spacing').value);
  _reportEditResult(FixtureEditor.addColumn('left', spacing));
});
document.getElementById('add-col-right-btn').addEventListener('click', () => {
  const spacing = parseFloat(document.getElementById('edit-col-spacing').value);
  _reportEditResult(FixtureEditor.addColumn('right', spacing));
});

// -- Step 4: calculate ---------------------------------------------------------

const calculateBtn = document.getElementById('calculate-btn');
const calcStatus = document.getElementById('calc-status');
const metricsTable = document.getElementById('metrics-table');

function updateCalculateEnabled() {
  calculateBtn.disabled = !(currentIesId && currentShape && currentFixtures && currentFixtures.length);
}

calculateBtn.addEventListener('click', async () => {
  if (!currentIesId || !currentShape || !currentFixtures) return;

  calcStatus.textContent = 'Calculating...';
  calcStatus.className = 'sc-status';

  // Blank override fields should mean "use the file's own value" - send
  // null rather than NaN so the backend's `in (None, "")` check treats
  // them as omitted (see api_routes.calculate()).
  const declaredLumensVal = document.getElementById('declared-lumens').value;
  const declaredWattsVal = document.getElementById('declared-watts').value;
  const ceilingHeightVal = document.getElementById('ceiling-height').value;

  const body = {
    ies_id: currentIesId,
    shape: buildShapeSpec(),
    fixtures: currentFixtures,
    mounting_height: parseFloat(document.getElementById('mounting-height').value),
    work_plane_height: parseFloat(document.getElementById('work-plane-height').value),
    grid_resolution: parseFloat(document.getElementById('grid-resolution').value),
    wall_margin: parseFloat(document.getElementById('measurement-margin').value),
    declared_lumens: declaredLumensVal === '' ? null : parseFloat(declaredLumensVal),
    declared_watts: declaredWattsVal === '' ? null : parseFloat(declaredWattsVal),
    ceiling_reflectance: parseFloat(document.getElementById('ceiling-reflectance').value) / 100,
    wall_reflectance: parseFloat(document.getElementById('wall-reflectance').value) / 100,
    floor_reflectance: parseFloat(document.getElementById('floor-reflectance').value) / 100,
    ceiling_height: ceilingHeightVal === '' ? null : parseFloat(ceilingHeightVal),
    maintenance_factor: parseFloat(document.getElementById('maintenance-factor').value),
    include_interreflection: document.getElementById('include-interreflection-checkbox').checked,
  };

  try {
    const res = await fetch('api/calculate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();

    if (!res.ok) {
      calcStatus.textContent = data.error || 'Calculation failed.';
      calcStatus.className = 'sc-status error';
      return;
    }

    calcStatus.textContent =
      `Done — ${data.grid_point_count} grid points, ${data.calc_time_seconds.toFixed(3)}s.`;
    calcStatus.className = 'sc-status ok';

    currentHeatmap = data.heatmap;
    _heatmapVersion++;
    currentGridResolution = data.grid_resolution;
    // The backend echoes fixtures back as {x, y, mounting_height} objects
    // (see api_routes.py's calculate response) - normalize back to [x, y]
    // pairs before storing, since that's the wire format /api/calculate
    // itself expects on the *next* call (sending the dicts back verbatim
    // crashes the backend with `pos[0]` on a dict - KeyError: 0).
    currentFixtures = data.fixtures.map((f) => (Array.isArray(f) ? [f[0], f[1]] : [f.x, f.y]));
    FixtureEditor.setFixtures(currentFixtures);

    // Pre-fill (never overwrite) the locked-scale bounds from the first
    // calculation, so turning "Lock color scale" on later already has a
    // sensible starting range to compare subsequent runs against.
    if (colorScaleMinInput.value === '') colorScaleMinInput.value = Math.floor(data.uniformity.e_min);
    if (colorScaleMaxInput.value === '') colorScaleMaxInput.value = Math.ceil(data.uniformity.e_max);

    render();

    document.getElementById('metric-fixtures').textContent = data.fixtures.length;
    document.getElementById('metric-gridpts').textContent = data.grid_point_count;
    document.getElementById('metric-emin').textContent = data.uniformity.e_min.toFixed(2);
    document.getElementById('metric-emax').textContent = data.uniformity.e_max.toFixed(2);
    document.getElementById('metric-eavg').textContent = data.uniformity.e_avg.toFixed(2);
    document.getElementById('metric-u0').textContent = data.uniformity.u0.toFixed(3);
    document.getElementById('metric-u1').textContent = data.uniformity.u1.toFixed(3);
    metricsTable.style.display = 'table';

    const envNote = document.getElementById('calc-environment-note');
    const po = data.photometric_output;
    const env = data.environment;
    const scaleNote = po.declared_lumens !== null
      ? `Rescaled to ${po.declared_lumens.toFixed(0)} lm (file declares ${po.file_total_lumens.toFixed(0)} lm) — flux scale &times;${po.flux_scale.toFixed(3)}.`
      : `Using file's own declared output (${po.file_total_lumens.toFixed(0)} lm) — no rescale applied.`;
    const efficacyNote = po.efficacy_lm_per_w !== null
      ? ` Efficacy: ${po.efficacy_lm_per_w.toFixed(1)} lm/W.`
      : '';
    const indirectNote = env.include_interreflection
      ? `Interreflection (approx., room-average, NOT per-point radiosity like DIALux): +${env.indirect_illuminance_lux.toFixed(1)} lux, from ${(env.ceiling_reflectance*100).toFixed(0)}% ceiling / ${(env.wall_reflectance*100).toFixed(0)}% wall / ${(env.floor_reflectance*100).toFixed(0)}% floor reflectance @ ${env.ceiling_height.toFixed(2)}m ceiling height.`
      : 'Interreflection disabled.';
    envNote.innerHTML =
      `${scaleNote}${efficacyNote}<br>${indirectNote}<br>Maintenance factor &times;${env.maintenance_factor.toFixed(2)} applied to the final result.`;
    envNote.style.display = 'block';
  } catch (err) {
    calcStatus.textContent = 'Network error during calculation.';
    calcStatus.className = 'sc-status error';
  }
});

// -- initial paint -------------------------------------------------------------

_recomputeActiveEditor();
_refreshHistoryButtons();
render();
