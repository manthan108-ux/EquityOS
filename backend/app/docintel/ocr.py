"""OCR with Tesseract, and the safeguards that stop OCR output from becoming "data" by itself.

Why the `tesseract` command and not a wrapper library: its TSV output carries a CONFIDENCE for
every word, which is how doubtful readings are flagged; it keeps OCR independent of any PDF
library; and a subprocess with a timeout cannot hang or crash the extractor.

Safety rules applied elsewhere (pipeline) on top of this module:

* a number read by OCR is never trusted because OCR returned it. On a scanned page a value is
  promoted only if (1) an accounting identity confirmed it, AND (2) when a second reading exists
  (the PDF's own embedded OCR text vs our Tesseract run) the two readings agree, AND (3) it has no
  look-alike characters (O/0, l/1, S/5) and no low-confidence word;
* anything doubtful is flagged `needs_review` and left out of the validated tables;
* header repairs (month names, units) change only text that is one edit from a known word, and only
  on OCR pages; values are never "repaired".
"""

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.docintel.layout import Word, looks_like_misread_number, parse_cell

TIMEOUT_SECONDS = 180
DEFAULT_DPI = 300
LOW_CONFIDENCE = 80.0  # Tesseract word confidence (0-100) below which a reading is doubtful
MAX_OUTPUT_BYTES = 20_000_000
_WINDOWS_DEFAULTS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


@dataclass(frozen=True)
class OcrWord(Word):
    conf: float = 100.0


def find_tesseract() -> str | None:
    """Path of the tesseract executable (PATH, TESSERACT_CMD, or the Windows default), or None."""
    explicit = os.environ.get("TESSERACT_CMD")
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("tesseract")
    if found:
        return found
    if sys.platform == "win32":
        return next((p for p in _WINDOWS_DEFAULTS if Path(p).is_file()), None)
    return None


def tesseract_version(exe: str | None = None) -> str | None:
    exe = exe or find_tesseract()
    if not exe:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [exe, "--version"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = (out.stdout or out.stderr).splitlines()
    return first[0].strip() if first else None


class OcrError(RuntimeError):
    """Tesseract could not read the image (message has no document content)."""


def ocr_png(png: bytes, *, dpi: int = DEFAULT_DPI, psm: int = 6) -> list[OcrWord]:
    """Words with boxes (PDF points) and confidences from a PNG rendered at `dpi`."""
    exe = find_tesseract()
    if exe is None:
        raise OcrError("Tesseract is not installed")
    cmd = [exe, "stdin", "stdout", "--psm", str(psm), "--dpi", str(dpi), "-l", "eng"]
    cmd += ["-c", "preserve_interword_spaces=1", "tsv"]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, image on stdin
            cmd, input=png, capture_output=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrError("Tesseract timed out") from exc
    except OSError as exc:
        raise OcrError(f"Tesseract could not start ({type(exc).__name__})") from exc
    if result.returncode != 0 or len(result.stdout) > MAX_OUTPUT_BYTES:
        raise OcrError(f"Tesseract failed (exit code {result.returncode})")
    return parse_tsv(result.stdout.decode("utf-8", errors="replace"), points_per_pixel=72 / dpi)


def parse_tsv(tsv: str, *, points_per_pixel: float) -> list[OcrWord]:
    """Word rows of Tesseract's TSV output (level 5) as OcrWords in PDF points."""
    words: list[OcrWord] = []
    for line in tsv.splitlines()[1:]:
        parts = line.split("\t", 11)
        if len(parts) < 12 or parts[0] != "5":
            continue
        text = parts[11].strip()
        if not text:
            continue
        try:
            left, top, width, height = (float(parts[i]) for i in (6, 7, 8, 9))
            conf = float(parts[10])
        except ValueError:
            continue
        if conf < 0:
            continue
        k = points_per_pixel
        words.append(OcrWord(left * k, top * k, (left + width) * k, (top + height) * k, text, conf))
    return words


def render_png(page, dpi: int = DEFAULT_DPI) -> bytes:
    """A PyMuPDF page as PNG bytes (rendering only; recognition is Tesseract's)."""
    return page.get_pixmap(dpi=dpi).tobytes("png")


def mean_confidence(words: Sequence[Word]) -> float | None:
    scored = [w.conf for w in words if isinstance(w, OcrWord)]
    return sum(scored) / len(scored) if scored else None


# ------------------------------------------------------------------------- doubtful readings


def ambiguous_characters(text: str) -> bool:
    """A number-like token mixing digits with look-alike letters ("1O,5O0.00", "l,234", "S0.5")."""
    return looks_like_misread_number(text)


def words_in_box(
    words: Sequence[Word], bbox: Sequence[float], pad_x: float = 3.0, pad_y: float = 2.0
):
    x0, y0, x1, y1 = bbox
    return [
        w for w in words if x0 - pad_x <= w.xc <= x1 + pad_x and y0 - pad_y <= w.yc <= y1 + pad_y
    ]


@dataclass(frozen=True)
class RegionReading:
    text: str
    min_conf: float | None
    ambiguous: bool


def read_region(words: Sequence[Word], bbox: Sequence[float]) -> RegionReading | None:
    inside = sorted(words_in_box(words, bbox), key=lambda w: w.x0)
    if not inside:
        return None
    text = "".join(w.text for w in inside)
    scored = [w.conf for w in inside if isinstance(w, OcrWord)]
    return RegionReading(
        text, min(scored) if scored else None, any(ambiguous_characters(w.text) for w in inside)
    )


_BORDER_CHARS = re.compile(r"[|\[\]{}\s]")
_COMMA_AS_POINT = re.compile(r"^\(?-?\d{1,3}(\.\d{3})+\)?$")


def cross_check(
    value: Decimal, bbox: Sequence[float], other_words: Sequence[Word]
) -> tuple[str, str]:
    """Compare a value with a SECOND, independent reading of the same page.

    Returns (AGREE | DISAGREE | NOT_FOUND, what the other reading saw). Two harmless differences
    are tolerated because they are artefacts of the page, not of the number: table border strokes
    read as brackets or bars ("29,550]"), and a thousands comma read as a decimal point
    ("95.754" for 95,754) - the latter only when the value is a whole number and every digit is
    identical. A different digit, a missing sign or a missing number is a DISAGREEMENT.
    """
    reading = read_region(other_words, bbox)
    if reading is None:
        return "NOT_FOUND", ""
    cleaned = _BORDER_CHARS.sub("", reading.text)
    readings = [parse_cell(cleaned)]
    if _COMMA_AS_POINT.match(cleaned) and value == value.to_integral_value():
        readings.append(parse_cell(cleaned.replace(".", ",")))
    return ("AGREE" if value in readings else "DISAGREE"), reading.text


# ------------------------------------------------------------------------- header repairs

_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
# every spelling a header may use -> month number
_NAME_TO_NUMBER = {name: i + 1 for i, name in enumerate(_MONTH_NAMES)}
_NAME_TO_NUMBER.update({name[:3]: i + 1 for i, name in enumerate(_MONTH_NAMES)})
_NAME_TO_NUMBER["sept"] = 9


def _edit_distance_at_most_one(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1 :] == short for i in range(len(long_)))


def repair_month(token: str) -> str | None:
    """'Seo' -> 'Sep': only if the token is not a month already and exactly ONE month (by number)
    is a single edit away. Always returns the three-letter form."""
    low = token.lower()
    if len(low) < 3 or low in _NAME_TO_NUMBER:
        return None
    numbers = {
        number
        for name, number in _NAME_TO_NUMBER.items()
        if len(name) >= 3 and _edit_distance_at_most_one(low, name)
    }
    if len(numbers) != 1:
        return None
    abbr = _MONTH_NAMES[next(iter(numbers)) - 1][:3]
    return abbr.capitalize() if token[:1].isupper() else abbr


def repair_header(text: str) -> tuple[str, list[str]]:
    """Fix single-character OCR slips in month names of a column header.

    Returns (text, ["Seo'25 -> Sep'25", ...]). Days, years and amounts are never touched.
    """
    notes: list[str] = []

    def fix(match: re.Match) -> str:
        token = match.group(1)
        fixed = repair_month(token)
        if fixed is None:
            return match.group(0)
        notes.append(f"{token} -> {fixed}")
        return match.group(0).replace(token, fixed, 1)

    text = re.sub(r"(?<![A-Za-z])([A-Za-z]{3,4})(?=['’]?\s?\d{2,4}\b)", fix, text)

    # The superscript ordinal of a day ("30th") is read as 30%, 3074, 301h, 307H, 31*1 ...: keep
    # the day, drop the junk. Only when a month name and a year follow, and the result must still
    # be a valid month-end day (checked later by the period parser), so a real misread of the
    # digits themselves is refused instead of accepted.
    def day(match: re.Match) -> str:
        notes.append(f"{match.group(0).strip()} -> {match.group(1)}th")
        return f"{match.group(1)}th "

    repaired = re.sub(
        r"\b(\d{2})(?:[%@#*•°º]\d?|[1lI7][hH4]|\d?[hH]|7\d?)\s*(?=(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]{0,6}\W{0,2}\s?\d{2,4})",
        day,
        text,
        flags=re.IGNORECASE,
    )
    return repaired, notes
