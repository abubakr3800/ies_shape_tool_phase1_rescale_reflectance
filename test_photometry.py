"""
test_photometry.py
------------------
Standalone sanity checks for ies_parser.py + photometry.py + aggregator.py,
runnable directly with `python test_photometry.py` - no pytest/Flask
required. This is the "physics sanity check" from the engineering plan's
applicability test plan (section 7, item 1), checked against
sample_data/sample_floodlight.ies, whose candela values are simple enough
to verify by hand.
"""

import math

from aggregator import summarize
from ies_parser import parse_ies_file
from models import Fixture
from photometry import interpolate_candela, point_illuminance, total_illuminance

SAMPLE_FILE = "sample_data/sample_floodlight.ies"


def test_parse_basic_fields():
    ies = parse_ies_file(SAMPLE_FILE)
    assert ies.lamp_count == 1
    assert ies.lumens_per_lamp == 3000
    assert ies.total_lumens == 3000
    assert ies.vertical_angles == [0, 10, 20, 30, 40, 50, 60, 70, 80, 90]
    assert ies.horizontal_angles == [0, 90]
    assert len(ies.candela) == 2          # 2 horizontal-angle blocks
    assert len(ies.candela[0]) == 10       # 10 vertical values per block
    assert ies.candela[0][0] == 5000        # nadir candela
    print("test_parse_basic_fields: OK")


def test_interpolate_candela_matches_table():
    ies = parse_ies_file(SAMPLE_FILE)
    # Exact table values, any horizontal angle (rotationally symmetric).
    assert interpolate_candela(ies, 0, 0) == 5000
    assert interpolate_candela(ies, 0, 45) == 5000   # symmetric -> same at any azimuth
    assert interpolate_candela(ies, 90, 0) == 0
    # Halfway between 0 and 10 degrees vertical -> halfway between 5000 and 4900.
    mid = interpolate_candela(ies, 5, 0)
    assert abs(mid - 4950) < 1e-6
    print("test_interpolate_candela_matches_table: OK")


def test_point_illuminance_directly_below_fixture():
    """Directly below the fixture, theta = 0, cos(theta) = 1, so
    E = candela(0) / d^2 exactly - easy to verify by hand."""
    ies = parse_ies_file(SAMPLE_FILE)
    fixture = Fixture(x=0.0, y=0.0, mounting_height=3.0, ies=ies)

    e = point_illuminance(fixture, 0.0, 0.0, work_plane_z=0.0)
    expected = 5000 / (3.0 ** 2)  # 555.555... lux
    assert abs(e - expected) < 1e-6, f"expected {expected}, got {e}"
    print(f"test_point_illuminance_directly_below_fixture: OK ({e:.3f} lux)")


def test_point_illuminance_off_axis_matches_manual_formula():
    """Pick an off-axis point and verify against the formula computed by
    hand with Python's own math functions, independent of the module's
    internal interpolation implementation."""
    ies = parse_ies_file(SAMPLE_FILE)
    fixture = Fixture(x=0.0, y=0.0, mounting_height=3.0, ies=ies)

    px, py = 3.0, 0.0  # 3m horizontally offset
    vertical_distance = 3.0
    d = math.sqrt(px ** 2 + py ** 2 + vertical_distance ** 2)
    cos_theta = vertical_distance / d
    theta_deg = math.degrees(math.acos(cos_theta))  # should be 45 degrees

    assert abs(theta_deg - 45.0) < 1e-6

    # candela at 45 deg vertical: halfway between the 40 (3400) and 50 (2600) table rows
    expected_candela = 3400 + (2600 - 3400) * 0.5  # 3000
    expected_e = (expected_candela * cos_theta) / (d ** 2)

    e = point_illuminance(fixture, px, py, work_plane_z=0.0)
    assert abs(e - expected_e) < 1e-6, f"expected {expected_e}, got {e}"
    print(f"test_point_illuminance_off_axis_matches_manual_formula: OK ({e:.3f} lux)")


def test_total_illuminance_two_identical_fixtures_doubles_it():
    ies = parse_ies_file(SAMPLE_FILE)
    f1 = Fixture(x=-1.0, y=0.0, mounting_height=3.0, ies=ies)
    f2 = Fixture(x=1.0, y=0.0, mounting_height=3.0, ies=ies)

    # At the midpoint (0,0), both fixtures are equidistant, so the total
    # should be exactly double a single fixture's contribution there.
    single = point_illuminance(f1, 0.0, 0.0, work_plane_z=0.0)
    total = total_illuminance([f1, f2], 0.0, 0.0, work_plane_z=0.0)
    assert abs(total - 2 * single) < 1e-9
    print(f"test_total_illuminance_two_identical_fixtures_doubles_it: OK ({total:.3f} lux)")


def test_aggregator_matches_definitions():
    values = [100.0, 150.0, 200.0]
    result = summarize(values)
    assert result.e_min == 100.0
    assert result.e_max == 200.0
    assert abs(result.e_avg - 150.0) < 1e-9
    assert abs(result.u0 - (100.0 / 150.0)) < 1e-9
    assert abs(result.u1 - (100.0 / 200.0)) < 1e-9
    print("test_aggregator_matches_definitions: OK")


if __name__ == "__main__":
    test_parse_basic_fields()
    test_interpolate_candela_matches_table()
    test_point_illuminance_directly_below_fixture()
    test_point_illuminance_off_axis_matches_manual_formula()
    test_total_illuminance_two_identical_fixtures_doubles_it()
    test_aggregator_matches_definitions()
    print("\nAll phase-1 sanity checks passed.")
