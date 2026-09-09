"""
ies_parser.py
-------------
Parses a standard IES LM-63 photometric file into an `IesData` object
(see models.py). This is the only module that needs to know about the raw
.ies text format — everything downstream just uses `IesData`.

IES file layout (the parts we care about), in order:

    IESNA:LM-63-...                 <- format line (optional/varies)
    [KEYWORD] value                  <- keyword lines, any number of them
    ...
    TILT=NONE | INCLUDE | <file>       <- tilt line, always present

    -- if TILT=INCLUDE, immediately followed by --
    lamp_to_luminaire_geometry
    num_tilt_pairs
    <num_tilt_pairs angles>
    <num_tilt_pairs multiplying factors>

    -- then the main data block (all whitespace-separated, can wrap across
       lines arbitrarily — we don't rely on line boundaries at all here) --
    num_lamps lumens_per_lamp multiplier Nv Nh photometric_type units_type
        width length height
    ballast_factor future_use input_watts
    <Nv vertical angles, ascending>
    <Nh horizontal angles, ascending>
    <Nv * Nh candela values, one block of Nv values per horizontal angle>

Known v1 limitation: TILT=INCLUDE data is consumed (so token alignment
stays correct) but the tilt multiplying factors are NOT applied to the
candela values. This only matters for fixtures mounted at an angle to
their own photometric axis (rare for the straight-down floodlight/highbay
case this tool targets first) — flagged here and in the engineering plan
rather than silently guessed at.
"""

from typing import List, Tuple

from models import IesData


class IesParseError(ValueError):
    """Raised when a file doesn't look like a valid IES file, instead of
    letting a confusing IndexError/ValueError bubble up from deep in the
    tokenizer."""


def parse_ies(content: str, source_filename: str = "") -> IesData:
    """Parse the full text of an .ies file into an IesData object.

    `content` is the raw file text (already decoded to str). Raises
    IesParseError with a human-readable message if the file can't be
    parsed, rather than an unrelated exception type.
    """
    lines = content.splitlines()
    if not lines:
        raise IesParseError("File is empty.")

    tilt_line_index, tilt_value = _find_tilt_line(lines)

    # Everything from the line *after* TILT= onward is just a stream of
    # whitespace-separated tokens - line breaks inside the data section
    # are not meaningful, so we don't try to parse line-by-line here.
    remaining_text = "\n".join(lines[tilt_line_index + 1:])
    tokens = remaining_text.split()
    token_iter = iter(tokens)

    tilt_supported = True
    if tilt_value.upper() == "INCLUDE":
        tilt_supported = False
        _consume_tilt_include_block(token_iter)
    elif tilt_value.upper() != "NONE":
        # TILT=<filename> - an external tilt file. Not supported in v1;
        # proceed as if TILT=NONE so the rest of the file still parses.
        tilt_supported = False

    try:
        num_lamps = int(float(_next(token_iter, "num_lamps")))
        lumens_per_lamp = float(_next(token_iter, "lumens_per_lamp"))
        multiplier = float(_next(token_iter, "multiplier"))
        num_vertical_angles = int(float(_next(token_iter, "num_vertical_angles")))
        num_horizontal_angles = int(float(_next(token_iter, "num_horizontal_angles")))
        _photometric_type = int(float(_next(token_iter, "photometric_type")))
        _units_type = int(float(_next(token_iter, "units_type")))
        _width = float(_next(token_iter, "width"))
        _length = float(_next(token_iter, "length"))
        _height = float(_next(token_iter, "height"))

        ballast_factor = float(_next(token_iter, "ballast_factor"))
        _future_use = float(_next(token_iter, "future_use_or_bf_photometric"))
        input_watts = float(_next(token_iter, "input_watts"))

        vertical_angles = [
            float(_next(token_iter, "vertical_angle"))
            for _ in range(num_vertical_angles)
        ]
        horizontal_angles = [
            float(_next(token_iter, "horizontal_angle"))
            for _ in range(num_horizontal_angles)
        ]

        candela = _read_candela_matrix(
            token_iter, num_horizontal_angles, num_vertical_angles
        )
    except StopIteration as exc:
        raise IesParseError(f"File ended unexpectedly while reading {exc}.") from exc
    except ValueError as exc:
        raise IesParseError(f"Could not parse a numeric field: {exc}") from exc

    return IesData(
        lamp_count=num_lamps,
        lumens_per_lamp=lumens_per_lamp,
        multiplier=multiplier,
        vertical_angles=vertical_angles,
        horizontal_angles=horizontal_angles,
        candela=candela,
        ballast_factor=ballast_factor,
        input_watts=input_watts,
        source_filename=source_filename,
        tilt_supported=tilt_supported,
    )


def _find_tilt_line(lines: List[str]) -> Tuple[int, str]:
    """Return (index, value) of the TILT= line. Everything before it is
    the format line + keyword lines, which we don't need."""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.upper().startswith("TILT="):
            return i, stripped[len("TILT="):].strip()
    raise IesParseError("No TILT= line found — this doesn't look like an IES file.")


def _consume_tilt_include_block(token_iter) -> None:
    """Skip over inline TILT=INCLUDE data (geometry flag, pair count, then
    that many angles and that many multiplying factors) so the token
    stream lines up correctly for the main data block that follows."""
    _lamp_to_luminaire_geometry = _next(token_iter, "tilt_geometry")
    num_pairs = int(float(_next(token_iter, "tilt_num_pairs")))
    for _ in range(num_pairs):
        _next(token_iter, "tilt_angle")
    for _ in range(num_pairs):
        _next(token_iter, "tilt_factor")


def _read_candela_matrix(
    token_iter, num_horizontal_angles: int, num_vertical_angles: int
) -> List[List[float]]:
    """Candela values are stored as `num_horizontal_angles` blocks, each
    containing `num_vertical_angles` values (one horizontal angle's full
    vertical sweep per block). Returned as candela[h_index][v_index]."""
    matrix: List[List[float]] = []
    for _h in range(num_horizontal_angles):
        row = [
            float(_next(token_iter, "candela_value"))
            for _ in range(num_vertical_angles)
        ]
        matrix.append(row)
    return matrix


def _next(token_iter, field_name: str) -> str:
    try:
        return next(token_iter)
    except StopIteration:
        # Re-raise with the field name attached so the caller's
        # StopIteration handler can report *what* was expected.
        raise StopIteration(field_name)


def parse_ies_file(path: str) -> IesData:
    """Convenience wrapper: read a file from disk and parse it."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return parse_ies(content, source_filename=path)
