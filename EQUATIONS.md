# IES Shape Tool — Equations, Code Locations, and Technical Effects

Single reference for every calculation: the formula, where it lives, **what it changes** (one fixture–point pair, one grid point, some points, or the whole dataset), and the technical effect from `README.md`, `calculation_equations_and_diagnosis.md`, and the source modules themselves.

Phase-2 production path is `POST /api/calculate` (`api_routes.calculate`). Phase-1 `POST /api/heatmap` uses the same **direct** formula only — no rescale, no interreflection, no maintenance factor.

---

## How to read the “scope” column

| Scope | Meaning |
|---|---|
| **1 pair** | One fixture × one measurement point. Independent of the rest of the grid. |
| **1 point** | One grid point. May sum several fixtures. |
| **some points** | A subset of the grid (clipped shape, holes, wall margin, lattice gaps). |
| **all points (same add)** | One scalar added identically to every grid value. |
| **all points (relative)** | Uses the whole value set (min/max/mean); changing one point can move the summary or heatmap colors. |
| **full-room scalar** | One number for the room. Does not vary with \((x, y)\). |
| **metadata only** | Echoed in the API / UI. Does **not** enter illuminance. |

---

## Master equation (phase 2)

For every measurement point \(P\) on the work plane:

\[
E_{\text{final}}(P) = \bigl(E_{\text{direct}}(P) + E_{\text{indirect}}\bigr) \times \text{MF}
\]

| Piece | Scope | Code |
|---|---|---|
| \(E_{\text{direct}}(P)\) | **1 point** (sum of **1 pair** terms) | `photometry.total_illuminance` → `point_illuminance` |
| \(E_{\text{indirect}}\) | **all points (same add)** | `interreflection.average_indirect_illuminance` |
| \(\text{MF}\) | **all points (same multiply)** | `api_routes.calculate` |
| \(E_{\text{final}}(P)\) written to `point.value` | **1 point**, then the list feeds aggregator + heatmap | `api_routes.py` ~514–521 |

```514:521:api_routes.py
    for point in points:
        direct = photometry.total_illuminance(
            fixtures, point.x, point.y, work_plane_z=work_plane_height
        )
        point.value = (direct + indirect_illuminance) * maintenance_factor
```

**Technical effect:** this is the only place the three lighting pieces are combined. Downstream (`aggregator`, `heatmap_render`) never recompute physics; they only see `point.value`.

**Phase-1 difference:** `/api/heatmap` sets `point.value = total_illuminance(...)` with no \(E_{\text{indirect}}\) and no MF (`api_routes.py` ~259–262). Direct-only, one fixture, rectangle grid.

---

## Pipeline order

```
IES text
  → ies_parser.parse_ies                     IesData (candela, multiplier, header lumens)
  → photometry.total_flux                    Φ_file  (full-room scalar)
  → flux_scale = Φ_declared / Φ_file         full-room scalar (optional)
  → RoomShape (area, perimeter, clip)        some points + full-room scalars
  → generate_measurement_grid                some points (who exists)
  → for each P:
        E_direct(P) = Σ_f  I·cosθ / d²       1 point
  → E_indirect = Φ·ρ / (A·(1−ρ))             full-room scalar → all points (same add)
  → E_final = (E_direct + E_indirect) × MF   all points (same multiply)
  → E_min, E_avg, U0, U1                     all points (relative)
  → heatmap colors from vmin/vmax            all points (relative)
```

Cost (README): \(O(\text{grid points} \times \text{fixtures})\). No distance cutoff. Fine at prototype scale; first thing to revisit for large rooms (`README.md`, `test_shape_pipeline.py` performance check).

---

## 1. IES header flux — \(\Phi_{\text{file}}\)

\[
\Phi_{\text{file}} =
\begin{cases}
N_{\text{lamps}} \times \Phi_{\text{per lamp}} & \text{if } \Phi_{\text{per lamp}} \ge 0 \\
k_{\text{sym}} \displaystyle\iint I(\theta,\phi)\,\sin\theta\,d\theta\,d\phi \times m & \text{if absolute photometry }(\Phi_{\text{per lamp}} = -1)
\end{cases}
\]

\(m\) = IES universal multiplying factor (`IesData.multiplier`).

| | |
|---|---|
| **Code** | `models.IesData.total_lumens` (header product); `photometry.total_flux` (prefers header, else `_integrate_candela_to_lumens`) |
| **Scope** | **full-room scalar** — not a per-point field. Feeds rescale and \(\Phi_{\text{total}}\) for interreflection. |
| **Does not** | Drive \(E_{\text{direct}}\) by itself. Direct lux always comes from the candela matrix (`models.py` notes the IES convention that candela values are already absolute). |

**Sphere integral** (`photometry._integrate_candela_to_lumens`): trapezoidal rule on the published \((\theta,\phi)\) grid, then multiplied by symmetry:

| File publishes | \(k_{\text{sym}}\) |
|---|---|
| Full 0–360° | 1 |
| Half 0–180° | 2 |
| Quarter 0–90° | 4 |
| One vertical curve (rotationally symmetric) | \(2\pi\) |

**Technical effect:** header flux is the DIALux-style rescale reference. Integration is the rare fallback so absolute-photometry files still have a \(\Phi_{\text{file}}\) (`photometry.py` docstring; `calculation_equations_and_diagnosis.md` §1).

**Parser caveat:** `TILT=INCLUDE` / external tilt files are consumed so tokens stay aligned, but tilt multiplying factors are **not** applied. Flagged `tilt_supported: false` (`ies_parser.py`, `README.md`). Affects the **whole candela matrix** if that file type is used.

---

## 2. Declared-lumens rescale — \(\text{flux\_scale}\)

\[
\text{flux\_scale} =
\begin{cases}
1 & \text{declared\_lumens omitted} \\
\Phi_{\text{declared}} / \Phi_{\text{file}} & \Phi_{\text{file}} > 0 \\
1 & \Phi_{\text{file}} = 0 \text{ (no divide-by-zero)}
\end{cases}
\]

| | |
|---|---|
| **Code** | `api_routes.calculate` ~419–436; stored on every `Fixture.flux_scale` (`models.py`) |
| **Scope** | **full-room scalar**, then applied to **every 1-pair** candela lookup **and** to \(\Phi_{\text{total}}\) in interreflection. One override for all fixtures (they share one `ies_id`). |
| **Default** | `config.DEFAULT_DECLARED_LUMENS = None` → scale 1.0 |

Every later intensity is:

\[
I_{\text{eff}}(\theta,\phi) = I_{\text{raw}}(\theta,\phi) \times m \times \text{flux\_scale}
\]

**Technical effect:** same behaviour as DIALux editing a luminaire’s luminous flux: the **shape** of the distribution is unchanged; the **magnitude** of direct lux, indirect lux, \(E_{\min}/E_{\max}/E_{\text{avg}}\), and heatmap values all scale by `flux_scale` (direct) / enter \(\Phi_{\text{total}}\) (indirect). Uniformity ratios \(U_0, U_1\) are **invariant** to a global scale if MF and the flat indirect term scale with it (they do, because \(\Phi_{\text{total}}\) uses the same `flux_scale`).

**Watts are metadata only.** `declared_watts` is echoed as efficacy \(\text{lm}/\text{W}\). It does **not** feed illuminance (`api_routes.py` ~441–444).

---

## 3. Horizontal-angle fold (symmetry)

Incoming azimuth \(\phi\) is mapped into the slice the file published before interpolation.

| File max \(\phi\) | Fold |
|---|---|
| \(\le 90^\circ\) | Quarter: mirror into 0–90° inside each quadrant |
| \(\le 180^\circ\) | Half: mirror around 180° |
| else | Full 0–360°: use as-is |

| | |
|---|---|
| **Code** | `photometry._fold_horizontal_angle` |
| **Scope** | **1 pair** — only the \((\theta,\phi)\) used for that fixture–point lookup. |
| **Effect** | Wrong fold = wrong \(I\) at that point (typical for round floodlights/highbays that only publish 0–90°). No change to which grid points exist. |

---

## 4. Bilinear candela interpolation

Clamp \(\theta\) to the published vertical range (no extrapolation). Linear blend in \(\theta\) at two neighbouring \(\phi\) columns, then blend those two results in \(\phi\):

\[
\begin{aligned}
c_0 &= c_{00} + (c_{01}-c_{00})\,v \\
c_1 &= c_{10} + (c_{11}-c_{10})\,v \\
I &= \bigl(c_0 + (c_1-c_0)\,h\bigr) \times m
\end{aligned}
\]

| | |
|---|---|
| **Code** | `photometry.interpolate_candela` (+ `_interp_1d`, `bisect`) |
| **Scope** | **1 pair**. End-clamped: angles outside the table use the edge row/column, not an extrapolated beam. |
| **Verified** | `test_photometry.test_interpolate_candela_matches_table` — midpoint 5° between 5000 and 4900 → 4950. |

**Technical effect:** this is how a continuous direction from geometry becomes a candela value. Errors here scale that point’s direct lux only.

---

## 5. Direct point-by-point illuminance (core physics)

For fixture \(f\) and point \(P=(x,y,z)\):

\[
\begin{aligned}
\Delta x &= x - f_x \\
\Delta y &= y - f_y \\
\Delta z &= H_{\text{mount}} - z \\
d &= \sqrt{\Delta x^2 + \Delta y^2 + \Delta z^2} \\
\cos\theta &= \Delta z / d \qquad (\theta \text{ from nadir}) \\
\phi &= \operatorname{atan2}(\Delta y, \Delta x) \\
E_{\text{direct},f}(P) &= \frac{I(\theta,\phi)\,\cos\theta}{d^2}
\end{aligned}
\]

Guards: if \(\Delta z \le 0\) or \(d = 0\), contribution is **0** (no non-positive distance).

Sum over fixtures (linear superposition):

\[
E_{\text{direct}}(P) = \sum_f E_{\text{direct},f}(P)
\]

| | |
|---|---|
| **Code** | `photometry.point_illuminance` (1 pair); `photometry.total_illuminance` (1 point) |
| **Scope** | **1 pair** then **1 point**. Different \(P\) get different \(d\), \(\theta\), \(I\) → this is the **only** term that creates spatial pattern (hot spots under fixtures, dark corners). |
| **Units** | meters in, lux out (IES candela/lumen convention). |
| **Verified** | Nadir: \(E = I(0)/H^2\) (`test_photometry` 5000 / 3²). Off-axis 45° matches a hand formula. Two identical fixtures at the midpoint **exactly double** one fixture. |

**Physical meaning of the two factors** (`photometry.py` docstring; diagnosis §3):

- \(1/d^2\) — inverse square: same intensity spread over a larger sphere.
- \(\cos\theta\) — work plane is horizontal; a grazing ray covers more area, so lux drops.

**What this is:** the same **point method** DIALux uses for its **direct** component. **No reflected light.** README: this is the **one** illuminance method the tool uses — no analytic/hybrid shortcut.

**Known v1 limits** (README + `photometry.py`): fixtures aim **straight down**; no fixture yaw (`φ = 0` aligned with world \(+X\)). Tilt/`aim_vector` is an extension point, not implemented. Wrong aim would change **every 1-pair** \(I(\theta,\phi)\) for that fixture.

---

## 6. Room geometry used by the light math

These are not illuminance equations, but they decide **who is calculated** and the **areas** inside the indirect formula.

### 6a. Floor area and wall perimeter

\[
A_{\text{floor}} = A_{\text{ceiling}} = \texttt{shape.area()} \qquad
L_{\text{perimeter}} = \texttt{shape.perimeter()}
\]

| | |
|---|---|
| **Code** | `room_geometry.RoomShape.area` / `.perimeter` |
| **Scope** | **full-room scalars** for interreflection. |
| **Perimeter detail** | Exterior length only. **Holes are not** extra wall area (`room_geometry.py` ~376–381). A column reduces floor area (shapely) but does not add interior-wall bounce area in this model. |

**Technical effect:** understated \(A_{\text{wall}}\) or \(A_{\text{total}}\) **raises** \(E_{\text{indirect}}\) (diagnosis §7b). Concave rooms and holes are validated/repaired via shapely (`README.md`, `room_geometry.py`). Self-intersections fail loudly (`RoomShapeError`) rather than silently wrong area.

### 6b. Wall / hole erosion (who exists)

Measurement and fixture lattices are generated on the bounding box of an **eroded** region, then clipped with `.covers()` (boundary counts).

\[
\text{region} = \text{polygon.buffer}(-m_{\text{wall}},\ \text{join\_style}=\text{mitre})
\]

| | |
|---|---|
| **Code** | `RoomShape.eroded`; `grid_generator.generate_measurement_grid`; `fixture_layout.generate_lattice` |
| **Scope** | **some points** — points in the margin, outside a concave notch, or inside a hole **never get an \(E\)**. They do not enter \(E_{\min}/E_{\text{avg}}/U_0\). |
| **Default margin** | `config.DEFAULT_WALL_MARGIN_M = 0.5` (LuxScale “avoid walls by 0.5 m”). |

Empty region after erosion → `ValueError` (loud config error, not a silent empty grid).

**Technical effect (README / tests):** L-shape excludes the missing quadrant from **both** fixtures and measurement points; a hole excludes the obstacle from both (`test_shape_pipeline.py`). Using `.covers` not `.contains` prevents dropping points that sit exactly on the eroded edge.

### 6c. Actual grid step (not the requested spacing)

After rounding the point count:

\[
\Delta x = W_{\text{usable}} / (N_{\text{cols}} - 1),\quad
\Delta y = L_{\text{usable}} / (N_{\text{rows}} - 1)
\]

| | |
|---|---|
| **Code** | `grid_generator.generate_rectangular_grid` / `generate_measurement_grid` |
| **Scope** | **some points** — changes **where** samples sit, hence which lux values exist. Denser grid → smoother heatmap and more stable \(E_{\min}\) (less chance of missing a true dark spot). |
| **Default resolution** | `config.DEFAULT_GRID_SPACING_M = 0.5` |

Fixture **actual** spacing is a median of surviving lattice gaps, not nearest-neighbour (`fixture_layout._actual_spacing`). Clipping a neighbour **widens** the reported gap. README: reported spacing is recomputed, not an echo of the request.

Independent on purpose: fixture spacing ≠ measurement resolution.

---

## 7. Indirect illuminance (uniform bounce)

**Not a radiosity solver.** DIALux varies indirect light by patch. This tool computes **one room-average lux** and adds it to **every** grid point (`interreflection.py` docstring; diagnosis §4; README “what this is NOT”).

### Areas

\[
\begin{aligned}
A_{\text{wall}} &= L_{\text{perimeter}} \times H_{\text{ceiling}} \\
A_{\text{total}} &= A_{\text{floor}} + A_{\text{ceiling}} + A_{\text{wall}}
\end{aligned}
\]

\(H_{\text{ceiling}}\) defaults to **mounting height** if unset (flush / surface-mounted). Pendant fixtures need an explicit interior height (`api_routes.py` ~487–493).

### Area-weighted reflectance

\[
\rho_{\text{avg}} = \frac{A_f\rho_f + A_c\rho_c + A_w\rho_w}{A_{\text{total}}}
\]

Clamped to \([0,\ 0.95]\) so \(1-\rho\) never hits zero.

Defaults (`config.py`): ceiling **0.70**, walls **0.50**, floor **0.20** (typical DIALux / CIE indoor presets). Must be in \([0, 1]\).

### Infinite-bounce / lumen-method equilibrium

Each lumen that hits a surface is re-reflected forever:

\[
\rho + \rho^2 + \rho^3 + \cdots = \frac{\rho}{1-\rho}
\]

Total lamp flux (already rescaled):

\[
\Phi_{\text{total}} = \sum_f \Phi_{\text{file}}(f) \times \text{flux\_scale}
\]

Spread uniformly over all interior surfaces:

\[
E_{\text{indirect}} = \frac{\Phi_{\text{total}}\,\rho_{\text{avg}}}{A_{\text{total}}\,(1-\rho_{\text{avg}})}
\]

Degenerate \(A_{\text{total}} \le 0\) → returns **0** (do not block the direct calc).

| | |
|---|---|
| **Code** | `interreflection.average_indirect_illuminance`; applied in `api_routes.calculate` if `include_interreflection` (default **True**) |
| **Scope** | **full-room scalar** → **all points (same add)**. Does **not** change the **shape** of the direct field; it **lifts the floor** of every value by the same lux. |
| **Toggle off / \(\rho=0\)** | \(E_{\text{indirect}}=0\) → pure direct-only (diagnosis §7a). |

**Technical effect on uniformity (structural, not a bug):** a constant \(C\) added to every \(E\) raises \(E_{\min}\) and \(E_{\text{avg}}\) by the same amount, so \(U_0 = E_{\min}/E_{\text{avg}}\) moves toward 1 but **cannot** match real radiosity, which brightens dark corners **more** than already-bright points (diagnosis §4, §7b; `interreflection.py`). Diagnosis also notes this flat term can **slightly overstate** \(U_0/U_1\) vs a real radiosity calc in the module docstring; vs DIALux the remaining U0 **gap** is still typically an undershoot because DIALux’s spatial fill-in is stronger than a flat add.

**Average vs DIALux:** the formula is a known-coarse over-estimator vs zonal-cavity / radiosity, especially for non-cube rooms. Ceiling-height default and reflectance mismatch are the first two knobs (diagnosis §7b).

---

## 8. Maintenance factor

\[
E_{\text{final}}(P) = (\cdots) \times \text{MF}
\]

| | |
|---|---|
| **Code** | `api_routes.calculate` after direct + indirect; default `config.DEFAULT_MAINTENANCE_FACTOR = 0.80` |
| **Scope** | **all points (same multiply)**. Scales \(E_{\min}\), \(E_{\max}\), \(E_{\text{avg}}\) by MF. **Does not change** \(U_0\) or \(U_1\) (ratios). |
| **Meaning** | Dirt + lamp lumen depreciation over the maintenance cycle. Applied to the **final** result, matching DIALux (diagnosis §5). Must be \(> 0\). |

---

## 9. Aggregation / uniformity

\[
\begin{aligned}
E_{\min} &= \min_P E_{\text{final}}(P) \\
E_{\max} &= \max_P E_{\text{final}}(P) \\
E_{\text{avg}} &= \operatorname{mean}_P E_{\text{final}}(P) \\
U_0 &= E_{\min} / E_{\text{avg}} \quad \text{(overall uniformity; same definition as DIALux)} \\
U_1 &= E_{\min} / E_{\max} \quad \text{(diversity)}
\end{aligned}
\]

Empty list → `ValueError` (usually a clip/shape bug upstream). If \(E_{\text{avg}}=0\) or \(E_{\max}=0\), the matching ratio is 0.

| | |
|---|---|
| **Code** | `aggregator.summarize` |
| **Scope** | **all points (relative)**. No physics. Changing **who is on the grid** (margin, holes, resolution) changes these numbers even if the formula is unchanged. |
| **Verified** | `test_photometry.test_aggregator_matches_definitions` on `[100, 150, 200]`. |

**Technical effect (diagnosis §6):** definitions match DIALux. Any mismatch vs DIALux is **upstream** in \(E_{\text{final}}\), mainly the flat indirect model — not this stage.

---

## 10. Heatmap color (display only)

\[
t = \frac{E - E_{\min}}{E_{\max} - E_{\min}} \quad (0 \text{ if span}=0)
\]

\(t\) is linearly interpolated through `config.HEATMAP_COLOR_STOPS` (blue → cyan → … jet-like). Brand colors are page chrome only (`config.py`).

| | |
|---|---|
| **Code** | `heatmap_render.value_to_color` / `render_grid` |
| **Scope** | **all points (relative)**. Color is normalized to **this grid’s** min/max, not an absolute lux scale. A global MF or flux_scale that scales every value **does not** change colors if min and max scale together. Adding flat \(E_{\text{indirect}}\) **compresses** contrast (same add, smaller relative spread) so the map looks more uniform even though the underlying pattern is the same direct field. |

Frontend only draws precomputed hex (`README.md`); JS does not reimplement the scale.

---

## What does **not** enter the light math

| Item | Where | Scope |
|---|---|---|
| `declared_watts` / efficacy | `api_routes.calculate` photometric_output | metadata only |
| Canvas curve handles | `shape_editor.js` flattens Bezier → polyline before `/api/shape` | geometry input only; `room_geometry` never sees curves |
| Fixture drag / add row | `fixture_editor.js` | changes **positions** (then every 1-pair \(d,\theta,\phi\)); not a separate formula |
| Heatmap hex colors | `heatmap_render.py` | display; not lux |
| Phase-1 `/api/heatmap` | no rescale / ρ / MF | different product path |

---

## Effect map (quick)

| Equation / step | File | 1 pair | 1 point | Some points | All points same | Full dataset relative |
|---|---|---|---|---|---|---|
| \(\Phi_{\text{file}}\), flux_scale | `photometry.py`, `api_routes.py` | via \(I\) | | | also \(\Phi_{\text{total}}\) | scales lux; U0/U1 invariant if everything scales |
| Fold + bilinear \(I\) | `photometry.py` | ✓ | | | | |
| \(E = I\cos\theta/d^2\) | `photometry.py` | ✓ | ✓ (sum) | | | **creates** spatial pattern |
| Erosion / clip / holes | `room_geometry.py`, `grid_generator.py` | | | **who exists** | | changes min/avg/U0 |
| \(A\), \(\rho_{\text{avg}}\), \(E_{\text{indirect}}\) | `interreflection.py` | | | | **same lux add** | lifts avg; pulls U0 toward 1 |
| MF | `api_routes.py` | | | | **same multiply** | scales lux; U0/U1 unchanged |
| \(E_{\min}, U_0, U_1\) | `aggregator.py` | | | | | ✓ |
| Heatmap \(t\) | `heatmap_render.py` | | | | | ✓ (relative colors) |

---

## Defaults that change numbers (not formulas)

From `config.py` — changing these never requires hunting through equation code:

| Symbol | Default | Affects |
|---|---|---|
| Grid spacing | 0.5 m | sample locations → \(E_{\min}\) stability |
| Wall margin | 0.5 m | drops near-wall points (often the darkest) → **raises** \(E_{\min}\) and \(U_0\) |
| Ceiling / wall / floor \(\rho\) | 0.70 / 0.50 / 0.20 | \(E_{\text{indirect}}\) magnitude |
| MF | 0.80 | all lux × 0.8 |
| Include interreflection | True | on/off the flat add |
| Declared lumens | None (scale 1) | global intensity |

---

## Why DIALux numbers can disagree (from `calculation_equations_and_diagnosis.md`)

Direct-only math has been independently verified (`test_photometry.py`, prior LuxScale review). Remaining gaps are mostly **indirect**:

1. **\(\rho = 0\):** this tool is pure direct. DIALux U0 with \(\rho=0\) still often looks “too uniform” to be a true zero-bounce room; the comparison mainly confirms direct point-by-point, not bounce.
2. **Default \(\rho\):** this tool’s flat \(E_{\text{indirect}}\) has been seen **~2×** DIALux’s implied indirect on a sample room — first checks: set **real** `ceiling_height` (don’t use mounting height for pendants), then **match DIALux reflectances**, not only CIE presets.
3. **U0 still short after averages get closer:** expected. A flat add cannot reproduce per-patch brightening of corners. Matching DIALux U0 needs a radiosity-style module, not more tuning of this scalar (`calculation_equations_and_diagnosis.md` recommended next steps).

Treat \(E_{\text{indirect}}\) as an **average-illuminance correction**, not a spatial bounce field.

---

## Tests that lock the equations

| Test | What it proves |
|---|---|
| `test_photometry.py` | Parser fields; bilinear midpoint; nadir \(I/d^2\); off-axis hand formula; two fixtures double; aggregator definitions |
| `test_shape_pipeline.py` | Rectangle polygon path = naive rectangle; L-shape / hole clip; actual ≠ requested spacing; hexagon area closed-form; perf of point-by-point |

Run: `python test_photometry.py` then `python test_shape_pipeline.py` (`README.md`).
