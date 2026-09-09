/**
 * geometry_utils.js
 * -----------------
 * Small, dependency-free geometry helpers shared by the canvas editors.
 * Deliberately tiny — anything more involved (real validation, erosion,
 * etc.) stays server-side in room_geometry.py via shapely. This file only
 * needs a fast point-in-polygon test so the fixture editor can politely
 * skip placing fixtures outside the room when adding a row/column.
 */

/**
 * Ray-casting point-in-ring test. `ring` is a list of [x,y] or {x,y}
 * points; the ring is treated as implicitly closed (no need to repeat
 * the first point at the end).
 */
function _pointInRing(px, py, ring) {
  if (!ring || ring.length < 3) return false;
  let inside = false;
  const pts = ring.map((p) => (Array.isArray(p) ? p : [p.x, p.y]));
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [xi, yi] = pts[i];
    const [xj, yj] = pts[j];
    const intersects =
      yi > py !== yj > py &&
      px < ((xj - xi) * (py - yi)) / (yj - yi + 1e-12) + xi;
    if (intersects) inside = !inside;
  }
  return inside;
}

/**
 * True if (px, py) is inside `exterior` and outside every ring in
 * `holes`. Mirrors RoomShape.contains's "inside exterior, outside every
 * hole" semantics closely enough for editor-side UX checks (this is not
 * a substitute for server-side validation via /api/shape).
 */
function pointInShape(px, py, exterior, holes) {
  if (!_pointInRing(px, py, exterior)) return false;
  for (const hole of holes || []) {
    if (_pointInRing(px, py, hole)) return false;
  }
  return true;
}
