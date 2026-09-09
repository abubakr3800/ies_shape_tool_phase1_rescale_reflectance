/**
 * canvas_heatmap.js
 * -----------------
 * PHASE 2 (+ interactive-editor update). Pure rendering: given a
 * normalized shape dict (as returned by /api/shape, /api/layout and
 * /api/calculate — {area, bounds, exterior, holes}), a list of fixture
 * [x,y] positions, and (once calculated) a heatmap payload, draw it all
 * on a <canvas>.
 *
 * Split into small layer functions (computeViewTransform / drawHeatmapLayer
 * / drawShapeOutline / drawFixtureMarkers) rather than one monolithic
 * function, so shape_editor.js and fixture_editor.js can reuse the same
 * coordinate transform and layer under/over their own interactive overlays
 * (in-progress vertices, drag handles, hover rings) without duplicating
 * the fit-to-canvas math. `drawScene` is kept as a convenience wrapper
 * for any caller that just wants the old one-shot "draw everything"
 * behavior.
 *
 * Deliberately has no fetch/state logic in it - app.js owns that.
 */

/**
 * World-meters <-> canvas-pixels transform that fits `bounds` into the
 * canvas with `padding` pixels of margin, preserving aspect ratio, then
 * applies an optional interactive camera (zoom + pan) on top of that fit.
 * `camera` defaults to identity ({zoom:1, panX:0, panY:0}) so every
 * existing caller that doesn't know about zoom/pan keeps working exactly
 * as before. Returns both directions (toX/toY and fromX/fromY) since the
 * editors need to convert mouse pixel coordinates back into world meters.
 */
function computeViewTransform(canvas, bounds, padding, camera) {
  camera = camera || { zoom: 1, panX: 0, panY: 0 };
  const availableW = canvas.width - padding * 2;
  const availableH = canvas.height - padding * 2;
  const shapeW = Math.max(bounds.max_x - bounds.min_x, 1e-6);
  const shapeH = Math.max(bounds.max_y - bounds.min_y, 1e-6);
  const baseScale = Math.min(availableW / shapeW, availableH / shapeH);
  const scale = baseScale * camera.zoom;

  const offsetX = padding + (availableW - shapeW * scale) / 2 + camera.panX;
  const offsetY = padding + (availableH - shapeH * scale) / 2 + camera.panY;

  return {
    toX: (x) => offsetX + (x - bounds.min_x) * scale,
    toY: (y) => offsetY + (y - bounds.min_y) * scale,
    fromX: (px) => (px - offsetX) / scale + bounds.min_x,
    fromY: (py) => (py - offsetY) / scale + bounds.min_y,
    scale,
    bounds,
  };
}

// Backward-compatible alias (older callers referenced this name).
function _fitTransform(canvas, bounds, padding) {
  return computeViewTransform(canvas, bounds, padding);
}

function _strokePolygonRing(ctx, ring, toX, toY) {
  if (!ring || ring.length === 0) return;
  ctx.beginPath();
  ring.forEach(([x, y], i) => {
    const cx = toX(x);
    const cy = toY(y);
    if (i === 0) ctx.moveTo(cx, cy);
    else ctx.lineTo(cx, cy);
  });
  ctx.closePath();
  ctx.stroke();
}

/**
 * Build a Path2D tracing the room's exterior with every hole cut out,
 * in canvas-pixel space. Uses the "evenodd" fill rule (odd-numbered ring
 * = fill, even-numbered = hole) so a single path represents "inside the
 * room, outside every obstacle" regardless of how many holes there are.
 * Used to CLIP the heatmap layer to the exact room outline (see
 * `drawHeatmapLayer`) so its edge follows the polygon's true boundary -
 * smooth and angled/curved as drawn - instead of the stair-step edge you
 * get from square grid cells alone, whatever their size.
 */
function _shapeClipPath(transform, shape) {
  const { toX, toY } = transform;
  const path = new Path2D();
  const addRing = (ring) => {
    if (!ring || ring.length === 0) return;
    ring.forEach(([x, y], i) => {
      const cx = toX(x);
      const cy = toY(y);
      if (i === 0) path.moveTo(cx, cy);
      else path.lineTo(cx, cy);
    });
    path.closePath();
  };
  addRing(shape.exterior);
  (shape.holes || []).forEach(addRing);
  return path;
}

/** Faint reference grid (every `spacing` meters) — helps free-hand drawing
 * and dragging read as "to scale" instead of floating in empty space. */
function drawReferenceGrid(ctx, canvas, transform, spacing) {
  const { bounds, toX, toY } = transform;
  ctx.save();
  ctx.strokeStyle = '#1c1c1c';
  ctx.lineWidth = 1;
  const startX = Math.floor(bounds.min_x / spacing) * spacing;
  const startY = Math.floor(bounds.min_y / spacing) * spacing;
  for (let x = startX; x <= bounds.max_x + spacing; x += spacing) {
    const px = toX(x);
    ctx.beginPath();
    ctx.moveTo(px, 0);
    ctx.lineTo(px, canvas.height);
    ctx.stroke();
  }
  for (let y = startY; y <= bounds.max_y + spacing; y += spacing) {
    const py = toY(y);
    ctx.beginPath();
    ctx.moveTo(0, py);
    ctx.lineTo(canvas.width, py);
    ctx.stroke();
  }
  ctx.restore();
}

/**
 * Paint the heatmap's per-point color cells. When `shape` is given, the
 * whole layer is clipped to the room's exact outline (exterior minus
 * holes) first — so cells that would otherwise poke past a slanted or
 * curved wall, or leave a small gap short of it, are cut off cleanly
 * along the true boundary instead of showing the grid's square edges.
 * This only changes what's painted, never the underlying point values -
 * the accurate per-point lux figures (and their colors) are untouched.
 */
function drawHeatmapLayer(ctx, transform, heatmap, cellSizeMeters, shape) {
  if (!heatmap || !heatmap.points || !heatmap.points.length) return;
  const { toX, toY, scale } = transform;
  const points = heatmap.points;

  let cellW, cellH;
  if (cellSizeMeters > 0) {
    // Preferred: the caller knows the actual requested grid resolution
    // (echoed back by /api/calculate), so use it directly. Inferring cell
    // size from the data (the fallback below) is fragile for a rotated
    // or heavily-clipped shape: a slanted polygon clips out different
    // columns on different rows, so "the two smallest distinct x values
    // anywhere in the results" can be many resolutions apart even though
    // the true spacing is fine - that produced oversized, gappy-looking
    // cells instead of a smooth fine grid.
    cellW = cellSizeMeters * scale;
    cellH = cellSizeMeters * scale;
  } else {
    const xs = [...new Set(points.map((p) => p.x))].sort((a, b) => a - b);
    const ys = [...new Set(points.map((p) => p.y))].sort((a, b) => a - b);
    cellW = xs.length > 1 ? (xs[1] - xs[0]) * scale : scale * 0.9;
    cellH = ys.length > 1 ? (ys[1] - ys[0]) * scale : scale * 0.9;
  }

  // A little overlap between neighboring cells (rather than sizing them
  // to exactly the grid spacing) closes the hairline seams that
  // full-precision non-integer pixel positions otherwise leave between
  // adjacent fillRect calls, so the interior reads as one continuous
  // gradient instead of a faint grid pattern.
  const drawW = cellW + 0.75;
  const drawH = cellH + 0.75;

  if (shape) {
    ctx.save();
    ctx.clip(_shapeClipPath(transform, shape), 'evenodd');
  }

  points.forEach((p) => {
    ctx.fillStyle = p.color;
    const cx = toX(p.x);
    const cy = toY(p.y);
    ctx.fillRect(cx - drawW / 2, cy - drawH / 2, drawW, drawH);
  });

  if (shape) ctx.restore();
}

/** Perceived-brightness check (standard luma weights) so the printed
 * number always reads clearly against whatever heatmap color sits
 * underneath it, instead of picking a single fixed text color that
 * disappears on half the color scale. */
function _bestLabelTextColor(hexColor) {
  const r = parseInt(hexColor.slice(1, 3), 16);
  const g = parseInt(hexColor.slice(3, 5), 16);
  const b = parseInt(hexColor.slice(5, 7), 16);
  const luma = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return luma > 0.55 ? '#000000' : '#ffffff';
}

// Below this on-screen cell size, a lux number won't fit legibly - skip
// drawing labels rather than render illegible overlapping text.
const MIN_LABEL_CELL_PX = 26;

/**
 * Print the exact lux value at the center of every grid cell, on top of
 * the already-painted color layer. Returns whether labels were actually
 * drawn (false + a reason when the grid is too dense on-screen for text
 * to fit) so the caller can surface that to the user instead of silently
 * doing nothing.
 */
function drawHeatmapValueLabels(ctx, transform, heatmap, cellSizeMeters) {
  if (!heatmap || !heatmap.points || !heatmap.points.length) {
    return { drawn: false, reason: 'no-data' };
  }
  const { toX, toY, scale } = transform;

  let cellPx;
  if (cellSizeMeters > 0) {
    cellPx = cellSizeMeters * scale;
  } else {
    const xs = [...new Set(heatmap.points.map((p) => p.x))].sort((a, b) => a - b);
    cellPx = xs.length > 1 ? (xs[1] - xs[0]) * scale : scale;
  }

  if (cellPx < MIN_LABEL_CELL_PX) {
    return { drawn: false, reason: 'too-dense' };
  }

  ctx.save();
  ctx.font = `${Math.min(12, Math.floor(cellPx * 0.3))}px Poppins, sans-serif`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';

  heatmap.points.forEach((p) => {
    ctx.fillStyle = _bestLabelTextColor(p.color);
    ctx.fillText(Math.round(p.value).toString(), toX(p.x), toY(p.y));
  });

  ctx.restore();
  return { drawn: true, reason: null };
}

/**
 * Find the heatmap point nearest a canvas-pixel position (e.g. from a
 * mousemove event), for a hover readout. Returns null if there's no
 * heatmap yet or the cursor is further than half a cell from any point
 * (so hovering empty canvas margin doesn't snap to a distant point).
 */
function findNearestHeatmapPoint(transform, heatmap, canvasX, canvasY, cellSizeMeters) {
  if (!heatmap || !heatmap.points || !heatmap.points.length) return null;
  const { fromX, fromY, scale } = transform;
  const worldX = fromX(canvasX);
  const worldY = fromY(canvasY);

  const maxDist = (cellSizeMeters > 0 ? cellSizeMeters : 1 / scale) * 0.75;
  let best = null;
  let bestDistSq = maxDist * maxDist;

  heatmap.points.forEach((p) => {
    const dx = p.x - worldX;
    const dy = p.y - worldY;
    const distSq = dx * dx + dy * dy;
    if (distSq <= bestDistSq) {
      bestDistSq = distSq;
      best = p;
    }
  });

  return best;
}

/**
 * A second, independent grid drawn ON TOP of everything else (heatmap,
 * shape outline, fixtures) — a plain ruler at whatever spacing the user
 * chooses, with meter numbers along the top and left edges, so you can
 * read off a coordinate regardless of how fine or coarse the underlying
 * measurement grid is. Deliberately separate from `drawReferenceGrid`
 * (the faint always-on 1m backdrop grid drawn UNDER the heatmap) — this
 * one is the user-facing ruler, styled to stay legible over any heatmap
 * color, and is skipped (not throttled/truncated) if the chosen spacing
 * would draw more lines than are useful to read.
 */
function drawRulerOverlay(ctx, canvas, transform, bounds, spacing) {
  if (!spacing || spacing <= 0) return;
  const { toX, toY } = transform;

  const startX = Math.floor(bounds.min_x / spacing) * spacing;
  const startY = Math.floor(bounds.min_y / spacing) * spacing;

  ctx.save();
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.35)';
  ctx.lineWidth = 1;
  ctx.font = '11px Poppins, sans-serif';
  ctx.fillStyle = '#ffffff';

  for (let x = startX; x <= bounds.max_x + spacing; x += spacing) {
    const px = toX(x);
    if (px < 0 || px > canvas.width) continue;
    ctx.beginPath();
    ctx.moveTo(px, 0);
    ctx.lineTo(px, canvas.height);
    ctx.stroke();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillText(x.toFixed(spacing < 1 ? 1 : 0), px + 2, 2);
  }

  for (let y = startY; y <= bounds.max_y + spacing; y += spacing) {
    const py = toY(y);
    if (py < 0 || py > canvas.height) continue;
    ctx.beginPath();
    ctx.moveTo(0, py);
    ctx.lineTo(canvas.width, py);
    ctx.stroke();
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(y.toFixed(spacing < 1 ? 1 : 0), 2, py - 2);
  }

  ctx.restore();
}

/**
 * One-time-per-dataset index for O(1) nearest-point lookups (instead of
 * scanning every grid point on every mousemove — the linear scan is what
 * made the tooltip itself heavy on a fine grid). Points from
 * grid_generator sit on a regular lattice at `resolution` spacing, so a
 * lookup is just rounding to the nearest lattice index. Rebuilt only when
 * the heatmap object changes (checked by reference, cheap), not on every
 * call.
 */
const _pointIndexCache = { forHeatmap: null, map: null, minX: 0, minY: 0, res: 1 };

function _pointIndexFor(heatmap, resolution) {
  if (_pointIndexCache.forHeatmap === heatmap) return _pointIndexCache;

  const map = new Map();
  let minX = Infinity;
  let minY = Infinity;
  heatmap.points.forEach((p) => {
    if (p.x < minX) minX = p.x;
    if (p.y < minY) minY = p.y;
  });
  const res = resolution > 0 ? resolution : 1;
  heatmap.points.forEach((p) => {
    const ix = Math.round((p.x - minX) / res);
    const iy = Math.round((p.y - minY) / res);
    map.set(`${ix},${iy}`, p);
  });

  _pointIndexCache.forHeatmap = heatmap;
  _pointIndexCache.map = map;
  _pointIndexCache.minX = minX;
  _pointIndexCache.minY = minY;
  _pointIndexCache.res = res;
  return _pointIndexCache;
}

/** Indexed replacement for repeated hover lookups — same result as
 * `findNearestHeatmapPoint` but O(1) instead of O(number of grid points)
 * after the first call for a given heatmap. */
function findNearestHeatmapPointIndexed(transform, heatmap, canvasX, canvasY, resolution) {
  if (!heatmap || !heatmap.points || !heatmap.points.length) return null;
  const idx = _pointIndexFor(heatmap, resolution);
  const worldX = transform.fromX(canvasX);
  const worldY = transform.fromY(canvasY);
  const ix = Math.round((worldX - idx.minX) / idx.res);
  const iy = Math.round((worldY - idx.minY) / idx.res);
  return idx.map.get(`${ix},${iy}`) || null;
}

function drawShapeOutline(ctx, transform, shape) {
  if (!shape) return;
  const { toX, toY } = transform;
  ctx.lineWidth = 2;
  ctx.strokeStyle = '#eb1b26';
  _strokePolygonRing(ctx, shape.exterior, toX, toY);

  if (shape.holes && shape.holes.length) {
    ctx.save();
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = '#cccccc';
    shape.holes.forEach((hole) => _strokePolygonRing(ctx, hole, toX, toY));
    ctx.restore();
  }
}

function drawFixtureMarkers(ctx, transform, fixtures) {
  const { toX, toY } = transform;
  (fixtures || []).forEach((f) => {
    const fx = Array.isArray(f) ? f[0] : f.x;
    const fy = Array.isArray(f) ? f[1] : f.y;
    const cx = toX(fx);
    const cy = toY(fy);
    ctx.beginPath();
    ctx.arc(cx, cy, 5, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff';
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = '#000000';
    ctx.stroke();
  });
}

/**
 * Draw the full scene in one call. `payload` is expected to look like:
 *   {
 *     shape: {bounds, exterior, holes},   // required
 *     fixtures: [[x,y], ...] | [{x,y}, ...],  // optional
 *     heatmap: {points: [{x,y,value,color}], ...},  // optional
 *   }
 * `viewBounds` optionally overrides the bounds used to fit the scene
 * (defaults to `payload.shape.bounds`) — the interactive editors pass
 * their own bounds while a shape is still being drawn (before there's a
 * validated `shape` to derive bounds from).
 */
function drawScene(canvas, payload, viewBounds) {
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const shape = payload.shape;
  const bounds = viewBounds || (shape && shape.bounds);
  if (!bounds) return;

  const padding = 20;
  const transform = computeViewTransform(canvas, bounds, padding);

  drawHeatmapLayer(ctx, transform, payload.heatmap, null, shape);
  if (payload.showValueLabels) drawHeatmapValueLabels(ctx, transform, payload.heatmap, null);
  if (shape) drawShapeOutline(ctx, transform, shape);
  drawFixtureMarkers(ctx, transform, payload.fixtures);

  return transform;
}
