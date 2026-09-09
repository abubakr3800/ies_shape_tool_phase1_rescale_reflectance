# IES Shape Tool — Calculation Logic & Equations (Phase 1, rescale + reflectance)

Reviewed from: `photometry.py`, `interreflection.py`, `aggregator.py`, `api_routes.py`, `config.py`

---

## 1. Photometric rescale (`declared_lumens` → `flux_scale`)

If the user overrides the luminaire's declared output:

```
flux_scale = declared_lumens / file_total_lumens        (api_routes.py:436)
```

`file_total_lumens` (= Φ_file) comes from `photometry.total_flux()`:

```
Φ_file = lamp_count × lumens_per_lamp        (IES header, preferred — matches DIALux convention)
```

or, only if the file uses absolute photometry (`lumens_per_lamp == -1`), by numerically
integrating the candela matrix over the sphere:

```
Φ_file = k_sym · ∬ I(θ,φ) · sin(θ) dθ dφ · multiplier
```

using trapezoidal integration over the published (θ,φ) grid, then scaled by a
symmetry factor `k_sym` ∈ {1, 2, 4, 2π} depending on how much of the sphere the
file actually publishes (full / half / quarter / rotationally symmetric).

Every candela value used downstream is effectively:

```
I_eff(θ,φ) = I_raw(θ,φ) × multiplier × flux_scale
```

---

## 2. Candela interpolation

`interpolate_candela()` bilinearly interpolates the IES candela matrix at an
arbitrary (θ, φ):

```
I(θ,φ) = c00 + (c01 − c00)·v_frac                     … along θ, at φ_lo
         c10 + (c11 − c10)·v_frac                     … along θ, at φ_hi
I(θ,φ) = c0  + (c1  − c0 )·h_frac                      … blend across φ
I(θ,φ) = I(θ,φ) × multiplier
```

with φ first **folded** into whatever symmetry slice the file publishes
(quarter: mirrored into 0–90°, half: mirrored around 180°, full: used as-is).

---

## 3. Direct (point-by-point) illuminance — the core formula

For each fixture *f* and work-plane point *P(x,y,z)*:

```
Δx = x − f.x
Δy = y − f.y
Δz = f.mounting_height − z                (vertical distance to work plane)

d      = √(Δx² + Δy² + Δz²)               (slant distance, fixture → point)
cos θ  = Δz / d                            (θ measured from nadir)
θ_deg  = acos(cos θ)
φ_deg  = atan2(Δy, Δx)

I      = interpolate_candela(θ_deg, φ_deg) × flux_scale

E_direct(P) = (I × cos θ) / d²             ← inverse-square + cosine law
```

Total direct illuminance at a point = sum over all fixtures:

```
E_direct_total(P) = Σ_f  E_direct_f(P)
```

This is a **direct-only** calculation — no reflected light. (Matches "point
method" photometry, the same method DIALux uses for its direct component.)

---

## 4. Indirect (interreflected) illuminance — approximation, NOT DIALux's method

`interreflection.py` explicitly documents itself as **not** a radiosity
solver. DIALux solves per-patch radiosity (indirect light varies point to
point — brighter near a light-colored wall, dimmer in a far corner). This
tool instead computes a single **room-average** indirect value and adds the
*same* number to every grid point:

```
ρ_avg = (A_floor·ρ_floor + A_ceiling·ρ_ceiling + A_wall·ρ_wall) / A_total

A_ceiling = A_floor  = room floor area
A_wall    = perimeter × ceiling_height        (ceiling_height defaults to
                                                mounting_height if not set —
                                                i.e. assumes surface-mounted
                                                fixtures)
A_total   = A_floor + A_ceiling + A_wall

Φ_total   = Σ_f  total_flux(f.ies) × f.flux_scale

E_indirect = (Φ_total × ρ_avg) / (A_total × (1 − ρ_avg))
```

This is the classical **infinite-bounce flux-transfer / "lumen method"
equilibrium formula**: every lumen that lands on a surface contributes
`ρ + ρ² + ρ³ + … = ρ/(1−ρ)` of itself back into the space, spread
**uniformly**.

`E_indirect` is added identically to every grid point:

```
E_total(P) = (E_direct_total(P) + E_indirect) × maintenance_factor
```

---

## 5. Maintenance factor

```
E_final(P) = (E_direct_total(P) + E_indirect) × MF        (default MF = 0.80)
```

Applied uniformly, after the indirect term — matches DIALux's "maintenance
factor applied to the final result" convention.

---

## 6. Aggregation / uniformity (`aggregator.py`)

```
E_min = min(E_final over grid)
E_max = max(E_final over grid)
E_avg = mean(E_final over grid)

U0 = E_min / E_avg           (overall uniformity — same definition DIALux uses)
U1 = E_min / E_max           (diversity)
```

No issue with this stage — the *definitions* match DIALux; any mismatch is
upstream, in the E_final values feeding it.

---

## 7. Why your two comparisons behave the way they do

### 7a. ρ = 0 case (avg 256–512, U0 = 0.68 vs DIALux 0.89)

With reflectance zeroed, `E_indirect = 0`, so this is a **pure direct-only**
comparison. DIALux's U0 = 0.89 in an MF = 0.8, ρ = 0 setting is *not really
achievable with zero reflectance* physically — so this comparison mainly
tells you your **direct-only point-by-point math itself has already been
independently verified correct** (per prior LuxScale review — matches a
from-scratch reimplementation). The 0.68 vs 0.89 gap here is expected and
consistent: it's simply the missing indirect component, which in a real room
always raises uniformity by filling in dark corners.

### 7b. Default-reflectance case (avg 364–729, U0 = 0.78 vs DIALux 0.89 / 306)

This is the case worth digging into, because **your average overshoots
DIALux while your uniformity still undershoots it** — two errors pulling in
different directions:

**Average overshoot** — back-solving from your numbers (100 W case):
```
raw_direct_avg        = 256.44 / 0.8  = 320.55 lux   (before MF)
raw_(direct+indirect) = 364.46 / 0.8  = 455.58 lux   (before MF)
⇒ your tool's E_indirect ≈ 135.0 lux

DIALux implied total   = 306 / 0.8    = 382.5 lux    (before MF)
⇒ DIALux's implied indirect ≈ 382.5 − 320.55 ≈ 62.0 lux
```
Your tool's flat indirect term is **~2.2× larger** than what DIALux's result
implies. Two likely causes, in order of likelihood:

1. **`ceiling_height` mismatch.** The tool defaults `ceiling_height` to
   `mounting_height` (i.e. assumes the fixture is flush with the ceiling).
   If your test room's real ceiling is taller than the fixture's mounting
   height, `A_wall = perimeter × ceiling_height` is understated, which
   *raises* `ρ_avg` and *shrinks* `A_total` — both push `E_indirect` up.
   Set `ceiling_height` explicitly to the real interior height.
2. **Reflectance values don't match what was actually entered in DIALux.**
   The tool's defaults (ceiling 0.70 / wall 0.50 / floor 0.20) are typical
   presets, but if the DIALux project used different (e.g. lower) surface
   reflectances, the two are not comparable. Worth confirming the DIALux
   project's *Room → Reflectances* values against `config.py`'s defaults.
3. To a lesser extent, the formula itself (idealized diffuse-cavity /
   infinite-bounce approximation, no view factors) is a known-coarse
   over-estimator versus a real zonal-cavity or radiosity calc, especially
   for rooms whose proportions deviate a lot from cube-like.

**Uniformity still undershoots even after averages get closer** — this part
is **structural, not a bug**: because `E_indirect` is one flat number added
to *every* point, it raises `E_min` and `E_avg` by the *same* absolute
amount. That mechanically pulls `U0 = E_min/E_avg` toward 1, but it can
never fully close the gap to a real radiosity result, because real
inter-reflection physically brightens dim/far/corner points *more* than
already-bright points (those corners "see" more wall/ceiling reflectance
solid angle), which raises real U0 further than a flat add ever can. This
matches the earlier LuxScale finding: a full per-patch radiosity module was
built and tested against a real DIALux room, and even that only partially
closed the uniformity gap (U0 stayed similar to direct-only), while its
average indirect contribution (~29 lux) was itself *lower* than DIALux's
implied indirect (~46 lux) on that room — i.e. this mismatch between
average-magnitude and uniformity-shape is a recurring, not one-off, issue.

### Recommended next steps
1. Set `ceiling_height` explicitly (don't rely on the mounting-height
   default) and re-run the ρ-included case.
2. Confirm the exact reflectance % used in the DIALux project and mirror
   them exactly (not the tool's presets).
3. Treat `E_indirect` as an *average-illuminance* correction only — don't
   expect U0 to reach DIALux's value from it. If matching DIALux's U0
   matters for this deliverable, the flat-add model needs to be replaced
   with (or blended with) the patch-based radiosity module already
   prototyped in LuxScale, not tuned further as a scalar.
