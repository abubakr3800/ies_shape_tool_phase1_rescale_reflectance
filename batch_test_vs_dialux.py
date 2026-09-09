"""
batch_test_vs_dialux.py
------------------------
Run this ON YOUR MACHINE, in the same folder as app.py, while the Flask
server is already running (`python app.py` in another terminal, listening
on http://127.0.0.1:5000).

It calls /api/layout once to build the fixture lattice, then calls
/api/calculate for every (wattage x light-center-height x
include_interreflection) combination and writes everything -- the exact
request bodies and the full JSON responses -- to a timestamped .log file
next to this script. Send that .log file back and it'll have everything
needed to locate the remaining gap precisely.

BEFORE RUNNING: edit the CONFIG block below to match your actual DIALux
setup (room shape/units, fixture spacing/pattern, IES file). Everything
else is generated automatically.
"""

import json
import sys
from datetime import datetime

try:
    import requests
except ImportError:
    print("This script needs the 'requests' package: pip install requests")
    sys.exit(1)

BASE_URL = "http://127.0.0.1:5000/api"

# =========================== CONFIG -- EDIT ME ===========================

# Room shape. Use "rectangle" with width/length, or "polygon" with
# exterior/holes (same spec /api/shape accepts). UNITS MUST MATCH what you
# used in the UI -- if "100 * 120" in your message was meters, leave as-is;
# if it was something else, fix width/length here.
SHAPE = {"type": "rectangle", "width": 100.0, "length": 120.0}

# Fixture lattice generation (/api/layout). Match whatever spacing/pattern
# you actually used in the UI session, or leave as a single centered
# fixture by setting SINGLE_FIXTURE = True below.
SINGLE_FIXTURE = False
SPACING_X = 8.25
SPACING_Y = 7.34
PATTERN = "grid"          # "grid" or "staggered"
ROTATION_DEG = 0.0
WALL_MARGIN = 0.5

# Measurement grid resolution (finer = closer to DIALux's typical grid).
GRID_RESOLUTION = 0.5

# Work plane (0.0 = floor level; DIALux usually uses 0.0 or 0.75-0.85m for
# a desk plane -- set to whatever DIALux used for these results).
WORK_PLANE_HEIGHT = 0.0

# The two candidate heights to test against each other -- this is the
# light-center-height (7) vs mounting/storey-height (7.9) question.
HEIGHTS_TO_TEST = [7.0, 7.9]

# Room surface reflectances + maintenance factor (match your DIALux
# project settings if they differ from these CIE-typical defaults).
CEILING_REFLECTANCE = 0.70
WALL_REFLECTANCE = 0.50
FLOOR_REFLECTANCE = 0.20
MAINTENANCE_FACTOR = 0.80

# Test the flat interreflection add-on both on and off, to see how much
# of the gap (if any) it's responsible for.
INTERREFLECTION_TO_TEST = [True, False]

# lm/W efficacy is fixed per your DIALux comparison; wattages come from
# your three test cases. declared_lumens overrides the uploaded IES
# file's own output via the tool's rescale feature.
LM_PER_W = 200.0
WATTAGES = [100.0, 150.0, 200.0]

# DIALux reference results (MF=0.8 already applied), keyed by watt, for
# the final side-by-side table. {avg_lux, u0}
DIALUX_REFERENCE = {
    100.0: {"avg_lux": 306.0, "u0": 0.89},
    150.0: {"avg_lux": 459.0, "u0": 0.89},
    200.0: {"avg_lux": 613.0, "u0": 0.89},
}

# Leave None to auto-pick (uses the only saved file, or the most recently
# saved one if there are several -- check the log's "Available IES files"
# section and set this explicitly if it picks the wrong one).
IES_ID = "e10a1e6b-6a17-4dbf-aec6-98050c80a423"

# ===========================================================================


def log(f, msg=""):
    print(msg)
    f.write(msg + "\n")


def pick_ies_id(f):
    if IES_ID:
        return IES_ID
    resp = requests.get(f"{BASE_URL}/ies/list")
    resp.raise_for_status()
    files = resp.json().get("files", [])
    log(f, f"Available IES files: {json.dumps(files, indent=2)}")
    if not files:
        raise RuntimeError("No saved IES files found -- upload one via the UI first.")
    chosen = files[-1]["id"] if isinstance(files[-1], dict) else files[-1]
    log(f, f"Auto-picked ies_id = {chosen} (set IES_ID in CONFIG to override)\n")
    return chosen


def build_fixtures(f, ies_id):
    if SINGLE_FIXTURE:
        cx = SHAPE.get("width", 0) / 2
        cy = SHAPE.get("length", 0) / 2
        fixtures = [[cx, cy]]
        log(f, f"Using a single centered fixture at ({cx}, {cy})\n")
        return fixtures

    body = {
        "shape": SHAPE,
        "spacing_x": SPACING_X,
        "spacing_y": SPACING_Y,
        "pattern": PATTERN,
        "rotation_deg": ROTATION_DEG,
        "wall_margin": WALL_MARGIN,
    }
    log(f, "POST /api/layout")
    log(f, json.dumps(body, indent=2))
    resp = requests.post(f"{BASE_URL}/layout", json=body)
    resp.raise_for_status()
    data = resp.json()
    log(f, "Response:")
    log(f, json.dumps(data, indent=2))
    log(f, "")
    return data["positions"]


def run_case(f, ies_id, fixtures, watts, height, interreflection):
    declared_lumens = watts * LM_PER_W
    body = {
        "ies_id": ies_id,
        "shape": SHAPE,
        "fixtures": fixtures,
        "mounting_height": height,
        "work_plane_height": WORK_PLANE_HEIGHT,
        "grid_resolution": GRID_RESOLUTION,
        "wall_margin": WALL_MARGIN,
        "ceiling_reflectance": CEILING_REFLECTANCE,
        "wall_reflectance": WALL_REFLECTANCE,
        "floor_reflectance": FLOOR_REFLECTANCE,
        "maintenance_factor": MAINTENANCE_FACTOR,
        "include_interreflection": interreflection,
        "declared_lumens": declared_lumens,
        "declared_watts": watts,
    }
    label = f"{watts:.0f}W @ {LM_PER_W:.0f}lm/W | height={height} | interreflection={interreflection}"
    log(f, "=" * 78)
    log(f, f"CASE: {label}")
    log(f, "-" * 78)
    log(f, "POST /api/calculate")
    log(f, json.dumps(body, indent=2))
    resp = requests.post(f"{BASE_URL}/calculate", json=body)
    try:
        data = resp.json()
    except ValueError:
        log(f, f"Non-JSON response (status {resp.status_code}): {resp.text}")
        return None
    log(f, "Response:")
    log(f, json.dumps(data, indent=2))
    log(f, "")
    if resp.status_code != 200:
        log(f, f"!! Request failed with status {resp.status_code} -- see error above.\n")
        return None
    return data


def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = f"batch_test_results_{timestamp}.log"
    rows = []

    with open(log_path, "w", encoding="utf-8") as f:
        log(f, f"Batch test run started {datetime.now().isoformat()}")
        log(f, f"CONFIG: {json.dumps({k: v for k, v in globals().items() if k.isupper()}, indent=2, default=str)}\n")

        ies_id = pick_ies_id(f)
        fixtures = build_fixtures(f, ies_id)
        log(f, f"Fixture count: {len(fixtures)}\n")

        for watts in WATTAGES:
            for height in HEIGHTS_TO_TEST:
                for interreflection in INTERREFLECTION_TO_TEST:
                    data = run_case(f, ies_id, fixtures, watts, height, interreflection)
                    if data is None:
                        continue
                    u = data["uniformity"]
                    rows.append({
                        "watts": watts,
                        "height": height,
                        "interreflection": interreflection,
                        "e_avg": u["e_avg"],
                        "u0": u["u0"],
                        "indirect_lux": data["environment"]["indirect_illuminance_lux"],
                    })

        log(f, "=" * 78)
        log(f, "SUMMARY vs DIALux (MF=0.8 already applied on both sides)")
        log(f, "=" * 78)
        header = f"{'W':>6} {'height':>7} {'interrefl':>10} {'E_avg':>10} {'U0':>6} {'indirect':>9} | {'DIALux E_avg':>13} {'DIALux U0':>10}"
        log(f, header)
        log(f, "-" * len(header))
        for r in rows:
            ref = DIALUX_REFERENCE.get(r["watts"], {})
            log(f, f"{r['watts']:>6.0f} {r['height']:>7.2f} {str(r['interreflection']):>10} "
                    f"{r['e_avg']:>10.1f} {r['u0']:>6.3f} {r['indirect_lux']:>9.1f} | "
                    f"{ref.get('avg_lux', float('nan')):>13.1f} {ref.get('u0', float('nan')):>10.3f}")

        log(f, f"\nFull log written to {log_path}")

    print(f"\nDone. Send back: {log_path}")


if __name__ == "__main__":
    main()
