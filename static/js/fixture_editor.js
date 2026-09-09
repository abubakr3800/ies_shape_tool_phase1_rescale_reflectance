/**
 * fixture_editor.js
 * -----------------
 * GUI editing for the fixture list produced by Step 3 (either the
 * auto-generated lattice or manual positions): drag a fixture to move it,
 * click empty space in "add" mode to drop a new one, click a fixture in
 * "remove" mode to delete it, and "Add row/column" to duplicate the
 * outermost row/column of the current layout at the configured spacing —
 * the GUI equivalent of manually retyping the manual-positions JSON.
 *
 * Fixtures are kept as plain [x, y] pairs (matching the wire format
 * everywhere else in the app — /api/layout, /api/calculate, the manual
 * textarea) so nothing else in app.js has to know the editor exists.
 */

const FixtureEditor = (() => {
  const HIT_PX = 9;
  const ROW_GROUP_TOLERANCE = 0.05; // meters, for grouping fixtures into "the top row" etc.

  let fixtures = []; // [[x, y], ...]
  let mode = 'move'; // 'move' | 'add' | 'remove'
  let dragIndex = null;
  let hoverIndex = null;
  let onChange = () => {};
  let shapeForClipping = null; // {exterior, holes} — optional, used to skip out-of-shape adds

  function setOnChange(fn) {
    onChange = fn || (() => {});
  }

  function _notify() {
    onChange(fixtures);
  }

  function setFixtures(list) {
    fixtures = (list || []).map((f) => (Array.isArray(f) ? [f[0], f[1]] : [f.x, f.y]));
  }

  function getFixtures() {
    return fixtures.map((f) => [f[0], f[1]]);
  }

  function setMode(m) {
    mode = m;
  }

  function getMode() {
    return mode;
  }

  function setShapeForClipping(shape) {
    shapeForClipping = shape;
  }

  function _isInsideShape(x, y) {
    if (!shapeForClipping) return true;
    return pointInShape(x, y, shapeForClipping.exterior, shapeForClipping.holes || []);
  }

  // -- hit testing ------------------------------------------------------------

  function _hitTest(screenPt, transform) {
    for (let i = 0; i < fixtures.length; i++) {
      const [fx, fy] = fixtures[i];
      const dx = transform.toX(fx) - screenPt.x;
      const dy = transform.toY(fy) - screenPt.y;
      if (Math.hypot(dx, dy) <= HIT_PX) return i;
    }
    return null;
  }

  // -- pointer events -----------------------------------------------------------

  function pointerDown(worldPt, screenPt, transform) {
    const hit = _hitTest(screenPt, transform);

    if (mode === 'remove') {
      if (hit !== null) {
        fixtures.splice(hit, 1);
        _notify();
      }
      return;
    }

    if (mode === 'add') {
      if (hit === null) {
        fixtures.push([worldPt.x, worldPt.y]);
        _notify();
      }
      return;
    }

    // 'move' mode
    if (hit !== null) dragIndex = hit;
  }

  function pointerMove(worldPt, screenPt, transform) {
    hoverIndex = _hitTest(screenPt, transform);
    if (dragIndex !== null) {
      fixtures[dragIndex] = [worldPt.x, worldPt.y];
      _notify();
    }
  }

  function pointerUp() {
    dragIndex = null;
  }

  // -- add row / add column -----------------------------------------------------

  function _groupByAxis(axisIndex) {
    // axisIndex 0 = group by x (columns), 1 = group by y (rows)
    const groups = new Map();
    fixtures.forEach(([x, y]) => {
      const key = Math.round((axisIndex === 0 ? x : y) / ROW_GROUP_TOLERANCE);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push([x, y]);
    });
    return groups;
  }

  /**
   * Duplicate the row of fixtures at the min or max Y, offset by
   * `spacingY` in that same direction. Returns a status object so the
   * caller can report what happened (nothing added, some clipped, etc.)
   * without throwing for the common "shape doesn't extend that far" case.
   */
  function addRow(direction, spacingY) {
    if (!fixtures.length) return { added: 0, skipped: 0, reason: 'No fixtures yet to base a new row on.' };
    if (!spacingY || spacingY <= 0) return { added: 0, skipped: 0, reason: 'Spacing Y must be positive.' };

    const ys = fixtures.map((f) => f[1]);
    const targetY = direction === 'above' ? Math.max(...ys) : Math.min(...ys);
    const delta = direction === 'above' ? spacingY : -spacingY;
    const sourceRow = fixtures.filter((f) => Math.abs(f[1] - targetY) <= ROW_GROUP_TOLERANCE);

    let added = 0, skipped = 0;
    sourceRow.forEach(([x]) => {
      const ny = targetY + delta;
      if (_isInsideShape(x, ny)) {
        fixtures.push([x, ny]);
        added++;
      } else {
        skipped++;
      }
    });

    if (added) _notify();
    return { added, skipped };
  }

  /** Same idea, duplicating the leftmost/rightmost column instead. */
  function addColumn(direction, spacingX) {
    if (!fixtures.length) return { added: 0, skipped: 0, reason: 'No fixtures yet to base a new column on.' };
    if (!spacingX || spacingX <= 0) return { added: 0, skipped: 0, reason: 'Spacing X must be positive.' };

    const xs = fixtures.map((f) => f[0]);
    const targetX = direction === 'right' ? Math.max(...xs) : Math.min(...xs);
    const delta = direction === 'right' ? spacingX : -spacingX;
    const sourceCol = fixtures.filter((f) => Math.abs(f[0] - targetX) <= ROW_GROUP_TOLERANCE);

    let added = 0, skipped = 0;
    sourceCol.forEach(([, y]) => {
      const nx = targetX + delta;
      if (_isInsideShape(nx, y)) {
        fixtures.push([nx, y]);
        added++;
      } else {
        skipped++;
      }
    });

    if (added) _notify();
    return { added, skipped };
  }

  // -- rendering ----------------------------------------------------------------

  function render(ctx, transform) {
    fixtures.forEach(([x, y], i) => {
      const cx = transform.toX(x), cy = transform.toY(y);
      const isDragging = i === dragIndex;
      const isHover = i === hoverIndex;

      ctx.beginPath();
      ctx.arc(cx, cy, isDragging ? 7 : 5, 0, Math.PI * 2);
      ctx.fillStyle = mode === 'remove' ? '#eb1b26' : '#ffffff';
      ctx.fill();
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = '#000000';
      ctx.stroke();

      if (isHover || isDragging) {
        ctx.beginPath();
        ctx.arc(cx, cy, 9, 0, Math.PI * 2);
        ctx.strokeStyle = mode === 'remove' ? '#eb1b26' : '#7fd97f';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
    });
  }

  return {
    setOnChange, setFixtures, getFixtures, setMode, getMode, setShapeForClipping,
    pointerDown, pointerMove, pointerUp, addRow, addColumn, render,
  };
})();
