/**
 * shape_editor.js
 * ---------------
 * Interactive "draw the room on the canvas" tool, replacing hand-typed
 * vertex JSON as the primary way to define a polygon room shape (the JSON
 * textarea still exists as a paste-in fallback in app.js).
 *
 * Data model: a "ring" is `{ points: [{x, y, curveTo: {cx, cy} | null}],
 * closed: bool }`. `curveTo` on a point describes the edge FROM that
 * point TO the next one as a quadratic Bezier control point — storing it
 * on the point itself (rather than in a separate edge-index map) means it
 * travels naturally with vertex insertion/deletion. Everything is stored
 * in world meters; only rendering/hit-testing touches pixels, via the
 * `computeViewTransform` transform passed in from app.js.
 *
 * Interactions (all direct-manipulation, no modal "modes" beyond
 * exterior-vs-hole and drawing-vs-done):
 *   - Click empty space while drawing         -> add a vertex
 *   - Click near the ring's first vertex      -> close the ring
 *   - Drag a vertex handle                    -> move it
 *   - Drag an edge's midpoint handle          -> bow it into a curve
 *     (drag back straight to flatten it again)
 *   - Double-click a vertex                   -> delete it
 *   - Double-click an edge (not on a vertex)  -> insert a vertex there
 *   - Right-click a vertex                    -> delete it (alt affordance)
 *   - Type an exact length + Enter while drawing -> place the next vertex
 *     that far away, in whatever direction the mouse is currently aiming
 *     (a small CAD-style "aim with the mouse, type the distance" flow)
 *   - Click a placed edge's length label      -> type an exact length for
 *     that edge, moving its second vertex to match (works before or
 *     after the ring is closed, so a rough shape can be corrected later)
 *   - "Snap angle" option constrains the aimed direction to 15°/45°/90°
 *     steps while drawing, so square corners come out exactly square
 *
 * Curves are stored as design-time Bezier control points but are
 * FLATTENED into a dense polyline when handed to the backend
 * (`toShapeSpec`), since room_geometry.py only knows straight-edge
 * polygons — the backend never needs to know curves were involved.
 */

const ShapeEditor = (() => {
  const VERTEX_HIT_PX = 9;
  const CURVE_HANDLE_HIT_PX = 8;
  const EDGE_LABEL_HIT_PX = 10;
  const EDGE_LABEL_OFFSET_PX = 16;
  const CLOSE_RING_HIT_PX = 12;
  const CURVE_FLATTEN_SEGMENTS = 16;
  const CURVE_SNAP_BACK_PX = 6; // dragging a curve handle back near-straight flattens it

  const HISTORY_LIMIT = 50;

  let state = null;
  let onChange = () => {};
  let history = []; // stack of {exterior, holes} snapshots (deep clones)
  let historyIndex = -1; // points at the snapshot matching the current state

  function reset() {
    state = {
      exterior: { points: [], closed: false },
      holes: [], // list of rings, same shape as exterior
      activeRing: null, // reference to the ring currently being drawn (or null)
      snap: false,
      gridSize: 0.25,
      angleSnap: false,
      angleSnapDeg: 15,
      drag: null, // {ring, kind: 'vertex'|'curve', index}
      hoverPreview: null, // {x,y} world — rubber-band line while drawing
      selectedVertex: null, // {ring, index} — last-clicked vertex; anchor for delete/nudge
    };
    history = [_cloneRingsState()];
    historyIndex = 0;
  }
  reset();

  function setOnChange(fn) {
    onChange = fn || (() => {});
  }

  // -- undo/redo history -----------------------------------------------------
  // Snapshots cover only the committed shape data (exterior + holes), not
  // transient interaction state (drag, hover preview, selection) — undoing
  // steps back through what the shape *looked like*, not through individual
  // mouse events.

  function _cloneRingsState() {
    return JSON.parse(JSON.stringify({ exterior: state.exterior, holes: state.holes }));
  }

  function _pushHistory() {
    // Drop any redo branch beyond the current point before recording a new one.
    history = history.slice(0, historyIndex + 1);
    history.push(_cloneRingsState());
    if (history.length > HISTORY_LIMIT) history.shift();
    historyIndex = history.length - 1;
  }

  function _restoreFromHistory(snapshot) {
    const cloned = JSON.parse(JSON.stringify(snapshot));
    state.exterior = cloned.exterior;
    state.holes = cloned.holes;
    // Undo/redo operates on committed shape data; re-point at whichever
    // ring (if any) is still open so drawing can resume, rather than
    // trying to preserve object identity through the clone.
    const openHole = state.holes.find((h) => !h.closed);
    state.activeRing = openHole || (!state.exterior.closed ? state.exterior : null);
    state.drag = null;
    state.selectedVertex = null;
    state.hoverPreview = null;
    onChange(); // bypass _notify/_pushHistory - this restore IS the history move
  }

  function canUndo() {
    return historyIndex > 0;
  }

  function canRedo() {
    return historyIndex < history.length - 1;
  }

  function undo() {
    if (!canUndo()) return false;
    historyIndex--;
    _restoreFromHistory(history[historyIndex]);
    return true;
  }

  function redo() {
    if (!canRedo()) return false;
    historyIndex++;
    _restoreFromHistory(history[historyIndex]);
    return true;
  }

  function _notify() {
    _pushHistory();
    onChange();
  }

  function isActive() {
    return state.exterior.points.length > 0 || state.holes.length > 0;
  }

  function isDrawing() {
    return !!state.activeRing;
  }

  function hasClosedExterior() {
    return state.exterior.closed;
  }

  function setSnap(enabled, gridSize) {
    state.snap = enabled;
    if (gridSize) state.gridSize = gridSize;
  }

  function setAngleSnap(enabled, degrees) {
    state.angleSnap = enabled;
    if (degrees) state.angleSnapDeg = degrees;
  }

  function _snapPoint(x, y) {
    if (!state.snap) return { x, y };
    const g = state.gridSize;
    return { x: Math.round(x / g) * g, y: Math.round(y / g) * g };
  }

  /** The point that will actually be used for the NEXT vertex while
   * drawing, given a raw world-space mouse position: applies angle
   * snapping (relative to the ring's last point) when enabled, else grid
   * snapping when enabled, else the raw point. Used for both the live
   * rubber-band preview and the point actually placed on click, so the
   * preview always matches what you'll get. */
  function _effectiveDrawPoint(worldPt) {
    if (state.activeRing && state.activeRing.points.length && state.angleSnap) {
      const last = state.activeRing.points[state.activeRing.points.length - 1];
      const dx = worldPt.x - last.x;
      const dy = worldPt.y - last.y;
      const mag = Math.hypot(dx, dy);
      if (mag < 1e-9) return worldPt;
      const step = (Math.PI * state.angleSnapDeg) / 180;
      const angle = Math.round(Math.atan2(dy, dx) / step) * step;
      return { x: last.x + mag * Math.cos(angle), y: last.y + mag * Math.sin(angle) };
    }
    return _snapPoint(worldPt.x, worldPt.y);
  }

  // -- starting/finishing rings --------------------------------------------

  function startExterior() {
    state.exterior = { points: [], closed: false };
    state.holes = [];
    state.activeRing = state.exterior;
    _notify();
  }

  function startHole() {
    if (!state.exterior.closed) return; // need a finished room first
    const hole = { points: [], closed: false };
    state.holes.push(hole);
    state.activeRing = hole;
    _notify();
  }

  function undoLastPoint() {
    if (state.activeRing && state.activeRing.points.length) {
      state.activeRing.points.pop();
      _notify();
    }
  }

  function _closeActiveRing() {
    if (!state.activeRing) return;
    if (state.activeRing.points.length < 3) return; // not a real polygon yet
    state.activeRing.closed = true;
    state.activeRing = null;
    _notify();
  }

  function finishCurrentRing() {
    _closeActiveRing();
  }

  function clearAll() {
    reset();
    _notify();
  }

  function clearHoles() {
    state.holes = [];
    if (state.activeRing && state.activeRing !== state.exterior) state.activeRing = null;
    _notify();
  }

  /** Steps the in-progress ring back to idle without touching anything
   * already committed: an unfinished exterior is discarded back to empty
   * (so "Start Drawing" begins fresh), an unfinished hole is dropped from
   * `state.holes` entirely, and an already-closed exterior is left alone.
   * This is the Escape-key behavior — deliberately narrower than
   * `clearAll()`, which wipes a finished exterior too. */
  function cancelActiveRing() {
    if (!state.activeRing) return false;
    if (state.activeRing === state.exterior) {
      state.exterior = { points: [], closed: false };
    } else {
      const idx = state.holes.indexOf(state.activeRing);
      if (idx >= 0) state.holes.splice(idx, 1);
    }
    state.activeRing = null;
    state.hoverPreview = null;
    state.selectedVertex = null;
    _notify();
    return true;
  }

  // -- selection (last-clicked vertex) — anchor for delete/nudge -----------

  function hasSelectedVertex() {
    return !!state.selectedVertex;
  }

  /** Deletes the currently-selected vertex, if any. Same constraint as the
   * existing double-click/right-click delete: only on a closed ring with
   * more than 3 points, since dropping below a triangle isn't a polygon. */
  function deleteSelectedVertex() {
    const sel = state.selectedVertex;
    if (!sel) return { ok: false, reason: 'No vertex selected — click a corner first.' };
    const { ring, index } = sel;
    if (!(ring.closed && ring.points.length > 3)) {
      return { ok: false, reason: "Can't delete — need a closed shape with more than 3 corners." };
    }
    ring.points.splice(index, 1);
    state.selectedVertex = null;
    _notify();
    return { ok: true };
  }

  /** Nudges the currently-selected vertex by (dx, dy) meters. Used for the
   * arrow-key fine-adjustment shortcut. Returns false (no-op) if nothing
   * is selected, so callers can skip re-rendering. */
  function nudgeSelectedVertex(dx, dy) {
    const sel = state.selectedVertex;
    if (!sel) return false;
    const p = sel.ring.points[sel.index];
    if (!p) {
      state.selectedVertex = null;
      return false;
    }
    p.x += dx;
    p.y += dy;
    _notify();
    return true;
  }

  // -- exact-length placement and editing -----------------------------------

  /**
   * Place the next vertex exactly `length` meters from the ring's last
   * point, in whatever direction the mouse is currently aiming (falls
   * back to due "east" if the mouse hasn't moved over the canvas yet).
   * The CAD-style "aim with the mouse, type the distance" flow this
   * enables is the main answer to "I can't control each side's length" -
   * free-hand clicking only gets you an approximate distance, this gets
   * you exact.
   */
  function setNextSegmentLength(length) {
    if (!state.activeRing || !state.activeRing.points.length) {
      return { ok: false, reason: 'Start drawing a shape first (place at least one point).' };
    }
    if (!(length > 0)) {
      return { ok: false, reason: 'Length must be a positive number.' };
    }
    const last = state.activeRing.points[state.activeRing.points.length - 1];
    let dir = { x: 1, y: 0 };
    if (state.hoverPreview) {
      const dx = state.hoverPreview.x - last.x;
      const dy = state.hoverPreview.y - last.y;
      const mag = Math.hypot(dx, dy);
      if (mag > 1e-9) dir = { x: dx / mag, y: dy / mag };
    }
    state.activeRing.points.push({ x: last.x + dir.x * length, y: last.y + dir.y * length, curveTo: null });
    _notify();
    return { ok: true };
  }

  /**
   * Set an existing (already-placed) edge's exact length, keeping its
   * start point fixed and moving its end point along the same direction
   * to match. Works on any ring (drawing or already closed) so a
   * roughly-drawn shape can be corrected edge-by-edge afterward. Any
   * curve on that edge is reset to straight, since a curved edge doesn't
   * have a single well-defined "length" to set.
   */
  function setEdgeLength(ring, index, length) {
    if (!(length > 0)) return { ok: false, reason: 'Length must be a positive number.' };
    const n = ring.points.length;
    const p = ring.points[index];
    const next = ring.points[(index + 1) % n];
    const dx = next.x - p.x;
    const dy = next.y - p.y;
    const mag = Math.hypot(dx, dy) || 1;
    next.x = p.x + (dx / mag) * length;
    next.y = p.y + (dy / mag) * length;
    p.curveTo = null;
    _notify();
    return { ok: true };
  }

  function edgeLength(ring, index) {
    const n = ring.points.length;
    const p = ring.points[index];
    const next = ring.points[(index + 1) % n];
    return Math.hypot(next.x - p.x, next.y - p.y);
  }

  // -- bounds (for fitting the canvas while there's no validated shape yet) -

  function boundingBox() {
    const pts = [];
    const collect = (ring) => {
      ring.points.forEach((p) => {
        pts.push([p.x, p.y]);
        if (p.curveTo) pts.push([p.curveTo.cx, p.curveTo.cy]);
      });
    };
    collect(state.exterior);
    state.holes.forEach(collect);
    // Include the live rubber-band cursor point while drawing, so the view
    // zooms/re-fits to keep a long aimed-but-not-yet-placed edge on screen
    // instead of only reacting once the point actually lands.
    if (state.activeRing && state.hoverPreview) {
      pts.push([state.hoverPreview.x, state.hoverPreview.y]);
    }
    if (!pts.length) return null;
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    // A little breathing room so points near the edge of the drawn-so-far
    // extent aren't right up against the canvas border.
    const padX = Math.max((maxX - minX) * 0.1, 0.5);
    const padY = Math.max((maxY - minY) * 0.1, 0.5);
    return { min_x: minX - padX, min_y: minY - padY, max_x: maxX + padX, max_y: maxY + padY };
  }

  // -- flattening curves into a straight-edge polyline for the backend -----

  function _flattenRing(ring) {
    const out = [];
    const n = ring.points.length;
    for (let i = 0; i < n; i++) {
      const p = ring.points[i];
      out.push([p.x, p.y]);
      if (p.curveTo) {
        const next = ring.points[(i + 1) % n];
        for (let s = 1; s < CURVE_FLATTEN_SEGMENTS; s++) {
          const t = s / CURVE_FLATTEN_SEGMENTS;
          const mt = 1 - t;
          const x = mt * mt * p.x + 2 * mt * t * p.curveTo.cx + t * t * next.x;
          const y = mt * mt * p.y + 2 * mt * t * p.curveTo.cy + t * t * next.y;
          out.push([x, y]);
        }
      }
    }
    return out;
  }

  function toShapeSpec() {
    if (!state.exterior.closed || state.exterior.points.length < 3) {
      throw new Error('Finish drawing the room outline (at least 3 points, then close it) before validating.');
    }
    const unfinishedHole = state.holes.find((h) => !h.closed);
    if (unfinishedHole) {
      throw new Error('Finish or clear the in-progress hole before validating.');
    }
    return {
      type: 'polygon',
      exterior: _flattenRing(state.exterior),
      holes: state.holes.map((h) => _flattenRing(h)),
    };
  }

  // -- hit testing (pixel space) --------------------------------------------

  function _allRings() {
    const rings = [];
    if (state.exterior.points.length) rings.push(state.exterior);
    state.holes.forEach((h) => {
      if (h.points.length) rings.push(h);
    });
    return rings;
  }

  function _edgeMidpointWorld(ring, i) {
    const n = ring.points.length;
    const p = ring.points[i];
    const next = ring.points[(i + 1) % n];
    if (p.curveTo) {
      // Bezier at t=0.5 — matches what's actually drawn as the curve's midpoint.
      return {
        x: 0.25 * p.x + 0.5 * p.curveTo.cx + 0.25 * next.x,
        y: 0.25 * p.y + 0.5 * p.curveTo.cy + 0.25 * next.y,
      };
    }
    return { x: (p.x + next.x) / 2, y: (p.y + next.y) / 2 };
  }

  function _hitTestVertex(screenPt, transform) {
    for (const ring of _allRings()) {
      for (let i = 0; i < ring.points.length; i++) {
        const p = ring.points[i];
        const dx = transform.toX(p.x) - screenPt.x;
        const dy = transform.toY(p.y) - screenPt.y;
        if (Math.hypot(dx, dy) <= VERTEX_HIT_PX) return { ring, index: i };
      }
    }
    return null;
  }

  function _hitTestCurveHandle(screenPt, transform) {
    for (const ring of _allRings()) {
      if (!ring.closed && ring.points.length < 2) continue;
      const n = ring.points.length;
      const edgeCount = ring.closed ? n : n - 1;
      for (let i = 0; i < edgeCount; i++) {
        const mid = _edgeMidpointWorld(ring, i);
        const dx = transform.toX(mid.x) - screenPt.x;
        const dy = transform.toY(mid.y) - screenPt.y;
        if (Math.hypot(dx, dy) <= CURVE_HANDLE_HIT_PX) return { ring, index: i };
      }
    }
    return null;
  }

  /** For inserting a vertex: nearest point ON an edge segment (not just its
   * handle), so double-clicking anywhere along a long edge works, not just
   * near its midpoint. */
  function _hitTestEdgeInsertion(screenPt, transform) {
    let best = null;
    let bestDist = 14; // px
    for (const ring of _allRings()) {
      if (!ring.closed) continue;
      const n = ring.points.length;
      for (let i = 0; i < n; i++) {
        const p = ring.points[i];
        const next = ring.points[(i + 1) % n];
        if (p.curveTo) continue; // inserting mid-curve isn't well-defined; skip
        const ax = transform.toX(p.x), ay = transform.toY(p.y);
        const bx = transform.toX(next.x), by = transform.toY(next.y);
        const { dist, t } = _distToSegment(screenPt, { x: ax, y: ay }, { x: bx, y: by });
        if (dist < bestDist && t > 0.05 && t < 0.95) {
          bestDist = dist;
          best = { ring, index: i, t };
        }
      }
    }
    return best;
  }

  function _distToSegment(p, a, b) {
    const abx = b.x - a.x, aby = b.y - a.y;
    const len2 = abx * abx + aby * aby || 1e-9;
    let t = ((p.x - a.x) * abx + (p.y - a.y) * aby) / len2;
    t = Math.max(0, Math.min(1, t));
    const cx = a.x + t * abx, cy = a.y + t * aby;
    return { dist: Math.hypot(p.x - cx, p.y - cy), t };
  }

  /** Screen position for an edge's length label: offset away from the
   * curve-handle diamond (which sits exactly at the edge's midpoint) so
   * the two don't overlap and both stay clickable. */
  function _edgeLabelScreenPos(ring, i, transform) {
    const n = ring.points.length;
    const p = ring.points[i];
    const next = ring.points[(i + 1) % n];
    const mid = _edgeMidpointWorld(ring, i);
    const ax = transform.toX(p.x), ay = transform.toY(p.y);
    const bx = transform.toX(next.x), by = transform.toY(next.y);
    const mx = transform.toX(mid.x), my = transform.toY(mid.y);
    const dx = bx - ax, dy = by - ay;
    const len = Math.hypot(dx, dy) || 1;
    // Perpendicular to the edge, in screen space.
    const nx = -dy / len, ny = dx / len;
    return { x: mx + nx * EDGE_LABEL_OFFSET_PX, y: my + ny * EDGE_LABEL_OFFSET_PX };
  }

  /** All (ring, index) edges eligible for a length label: any edge with
   * at least two real points, whether the ring is closed yet or not (so
   * you can see/fix lengths edge-by-edge while still drawing). */
  function _labelableEdges() {
    const edges = [];
    for (const ring of _allRings()) {
      const n = ring.points.length;
      if (n < 2) continue;
      const edgeCount = ring.closed ? n : n - 1;
      for (let i = 0; i < edgeCount; i++) edges.push({ ring, index: i });
    }
    return edges;
  }

  function hitTestEdgeLabel(screenPt, transform) {
    for (const { ring, index } of _labelableEdges()) {
      const pos = _edgeLabelScreenPos(ring, index, transform);
      const dx = pos.x - screenPt.x, dy = pos.y - screenPt.y;
      if (Math.hypot(dx, dy) <= EDGE_LABEL_HIT_PX + 6) {
        return { ring, index, length: edgeLength(ring, index), screenPos: pos };
      }
    }
    return null;
  }

  // -- pointer event handlers (called from app.js with world + screen coords)

  function pointerDown(worldPt, screenPt, transform) {
    // "Close the ring being drawn" takes priority over "start dragging its
    // first vertex" when both are true of the same click — otherwise a
    // click on the very point that's supposed to close the shape just
    // grabs it for dragging instead.
    if (state.activeRing && state.activeRing.points.length >= 3) {
      const first = state.activeRing.points[0];
      const dx = transform.toX(first.x) - screenPt.x;
      const dy = transform.toY(first.y) - screenPt.y;
      if (Math.hypot(dx, dy) <= CLOSE_RING_HIT_PX) {
        _closeActiveRing();
        return;
      }
    }

    // Existing handles take priority over "add a new point". (Edge-label
    // clicks are handled by app.js before this is even called, since they
    // open a floating HTML input rather than starting a drag.)
    const vHit = _hitTestVertex(screenPt, transform);
    if (vHit) {
      state.selectedVertex = { ring: vHit.ring, index: vHit.index };
      state.drag = { ring: vHit.ring, kind: 'vertex', index: vHit.index };
      return;
    }
    // Any click that doesn't land on a vertex deselects — matches the
    // click-empty-space-to-deselect convention used elsewhere in the app.
    state.selectedVertex = null;
    const cHit = _hitTestCurveHandle(screenPt, transform);
    if (cHit) {
      state.drag = { ring: cHit.ring, kind: 'curve', index: cHit.index };
      return;
    }

    if (state.activeRing) {
      const placed = _effectiveDrawPoint(worldPt);
      state.activeRing.points.push({ x: placed.x, y: placed.y, curveTo: null });
      _notify();
    }
  }

  function pointerMove(worldPt, screenPt, transform) {
    if (state.drag) {
      const { ring, kind, index } = state.drag;
      if (kind === 'vertex') {
        const snapped = _snapPoint(worldPt.x, worldPt.y);
        ring.points[index].x = snapped.x;
        ring.points[index].y = snapped.y;
      } else if (kind === 'curve') {
        const n = ring.points.length;
        const p = ring.points[index];
        const next = ring.points[(index + 1) % n];
        const midX = (p.x + next.x) / 2;
        const midY = (p.y + next.y) / 2;
        // Snap-back-to-straight: if the drag lands close (in pixels) to
        // the straight-edge midpoint, flatten the curve instead of
        // creating a barely-there one.
        const midPx = { x: transform.toX(midX), y: transform.toY(midY) };
        if (Math.hypot(midPx.x - screenPt.x, midPx.y - screenPt.y) <= CURVE_SNAP_BACK_PX) {
          p.curveTo = null;
        } else {
          // Solve for the quadratic-Bezier control point such that the
          // curve's own midpoint (t=0.5) lands under the cursor:
          // B(0.5) = 0.25*p0 + 0.5*c + 0.25*p1  =>  c = 2*mouse - 0.5*(p0+p1)
          p.curveTo = {
            cx: 2 * worldPt.x - midX,
            cy: 2 * worldPt.y - midY,
          };
        }
      }
      _notify();
      return;
    }
    if (state.activeRing) {
      state.hoverPreview = _effectiveDrawPoint(worldPt);
    }
  }

  function pointerUp() {
    state.drag = null;
  }

  function doubleClick(screenPt, transform) {
    const vHit = _hitTestVertex(screenPt, transform);
    if (vHit && vHit.ring.closed && vHit.ring.points.length > 3) {
      vHit.ring.points.splice(vHit.index, 1);
      state.selectedVertex = null; // stale index after the splice
      _notify();
      return;
    }
    const insertion = _hitTestEdgeInsertion(screenPt, transform);
    if (insertion) {
      const { ring, index } = insertion;
      const p = ring.points[index];
      const next = ring.points[(index + 1) % ring.points.length];
      const mid = { x: (p.x + next.x) / 2, y: (p.y + next.y) / 2, curveTo: null };
      ring.points.splice(index + 1, 0, mid);
      state.selectedVertex = null; // stale index after the splice
      _notify();
    }
  }

  function rightClickDelete(screenPt, transform) {
    const vHit = _hitTestVertex(screenPt, transform);
    if (vHit && vHit.ring.closed && vHit.ring.points.length > 3) {
      vHit.ring.points.splice(vHit.index, 1);
      state.selectedVertex = null; // stale index after the splice
      _notify();
      return true;
    }
    return false;
  }

  // -- rendering -------------------------------------------------------------

  function _ringPath(ctx, ring, transform) {
    const n = ring.points.length;
    if (n === 0) return;
    ctx.beginPath();
    const first = ring.points[0];
    ctx.moveTo(transform.toX(first.x), transform.toY(first.y));
    const edgeCount = ring.closed ? n : n - 1;
    for (let i = 0; i < edgeCount; i++) {
      const p = ring.points[i];
      const next = ring.points[(i + 1) % n];
      if (p.curveTo) {
        ctx.quadraticCurveTo(
          transform.toX(p.curveTo.cx), transform.toY(p.curveTo.cy),
          transform.toX(next.x), transform.toY(next.y)
        );
      } else {
        ctx.lineTo(transform.toX(next.x), transform.toY(next.y));
      }
    }
  }

  function _drawLabel(ctx, x, y, text) {
    ctx.save();
    ctx.font = '11px "Poppins", sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const padX = 4, padY = 2;
    const w = ctx.measureText(text).width;
    ctx.fillStyle = 'rgba(13,13,13,0.85)';
    ctx.fillRect(x - w / 2 - padX, y - 7 - padY, w + padX * 2, 14 + padY * 2);
    ctx.fillStyle = '#ffffff';
    ctx.fillText(text, x, y);
    ctx.restore();
  }

  function _renderRing(ctx, ring, transform, { color, isHole }) {
    if (ring.points.length === 0) return;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    if (isHole) ctx.setLineDash([5, 4]);
    _ringPath(ctx, ring, transform);
    ctx.stroke();
    ctx.restore();

    // Rubber-band preview line from the last placed point to the cursor,
    // only while this ring is the one actively being drawn — with a live
    // length readout so you can see the exact distance before committing
    // to a click (or typing an exact length instead, via the toolbar).
    if (ring === state.activeRing && state.hoverPreview && ring.points.length) {
      const last = ring.points[ring.points.length - 1];
      ctx.save();
      ctx.strokeStyle = color;
      ctx.globalAlpha = 0.5;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(transform.toX(last.x), transform.toY(last.y));
      ctx.lineTo(transform.toX(state.hoverPreview.x), transform.toY(state.hoverPreview.y));
      ctx.stroke();
      ctx.restore();

      const liveLen = Math.hypot(state.hoverPreview.x - last.x, state.hoverPreview.y - last.y);
      const midX = (transform.toX(last.x) + transform.toX(state.hoverPreview.x)) / 2;
      const midY = (transform.toY(last.y) + transform.toY(state.hoverPreview.y)) / 2;
      _drawLabel(ctx, midX, midY - 12, `${liveLen.toFixed(2)} m`);
    }

    // Length labels for every already-placed edge (offset from the
    // curve-handle diamond, which sits at the exact midpoint) — click one
    // to type an exact replacement length.
    const n = ring.points.length;
    const edgeCount = ring.closed ? n : n - 1;
    for (let i = 0; i < edgeCount; i++) {
      const pos = _edgeLabelScreenPos(ring, i, transform);
      _drawLabel(ctx, pos.x, pos.y, `${edgeLength(ring, i).toFixed(2)} m`);
    }

    // Vertex handles.
    ring.points.forEach((p, i) => {
      const px = transform.toX(p.x), py = transform.toY(p.y);
      const isSelected = !!state.selectedVertex && state.selectedVertex.ring === ring && state.selectedVertex.index === i;
      ctx.beginPath();
      ctx.arc(px, py, isSelected ? 6.5 : 5, 0, Math.PI * 2);
      const isFirst = i === 0 && ring === state.activeRing;
      ctx.fillStyle = isSelected ? '#eb1b26' : (isFirst ? '#7fd97f' : '#ffffff');
      ctx.fill();
      ctx.lineWidth = isSelected ? 2 : 1.5;
      ctx.strokeStyle = '#000000';
      ctx.stroke();
    });

    // Curve handles (small diamonds at each edge's effective midpoint) —
    // only shown once the ring is closed, to keep the in-progress drawing
    // view uncluttered.
    if (ring.closed) {
      for (let i = 0; i < n; i++) {
        const mid = _edgeMidpointWorld(ring, i);
        const mx = transform.toX(mid.x), my = transform.toY(mid.y);
        ctx.save();
        ctx.translate(mx, my);
        ctx.rotate(Math.PI / 4);
        ctx.fillStyle = ring.points[i].curveTo ? '#eb1b26' : '#888888';
        ctx.fillRect(-3.5, -3.5, 7, 7);
        ctx.restore();
      }
    }
  }

  function render(ctx, transform) {
    if (state.exterior.points.length) {
      _renderRing(ctx, state.exterior, transform, { color: '#eb1b26', isHole: false });
    }
    state.holes.forEach((h) => _renderRing(ctx, h, transform, { color: '#cccccc', isHole: true }));
  }

  return {
    reset, clearAll, clearHoles, setOnChange, setSnap, setAngleSnap,
    startExterior, startHole, undoLastPoint, finishCurrentRing, cancelActiveRing,
    isActive, isDrawing, hasClosedExterior, boundingBox, toShapeSpec,
    setNextSegmentLength, setEdgeLength, edgeLength, hitTestEdgeLabel,
    pointerDown, pointerMove, pointerUp, doubleClick, rightClickDelete,
    hasSelectedVertex, deleteSelectedVertex, nudgeSelectedVertex,
    undo, redo, canUndo, canRedo,
    render,
  };
})();
