"""Read text and word positions from a PDF with PyMuPDF.

Safety: PDFs are untrusted. They are opened read-only from our content-addressed store; nothing in
them is executed (no JavaScript, no links followed, no embedded files opened); page count and
words per page are capped; a password-protected file is reported, never cracked.

Pages without a usable text layer (scans, or fonts whose characters cannot be mapped back to
text) are flagged `needs_ocr`. OCR (Tesseract, see `ocr.py`) runs ONLY on those pages and only if
Tesseract is installed; otherwise they are reported as unreadable rather than guessed. Text-based
pages are never OCR'd.

Page text methods: `text_layer` (born digital), `embedded_ocr` (a scan carrying someone else's
invisible OCR text), `ocr` (read by our Tesseract run), `none`.
"""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.docintel.layout import Word, group_lines
from app.docintel.ocr import (
    OcrError,
    find_tesseract,
    mean_confidence,
    ocr_png,
    render_png,
    tesseract_version,
)

MAX_PAGES = 2000
MAX_WORDS_PER_PAGE = 30000
MIN_TEXT_CHARS = 40  # fewer characters than this on a page with images: probably a scan
MIN_READABLE_SHARE = 0.6  # share of "normal" characters below this: unmappable font glyphs


@dataclass
class PageData:
    number: int  # 1-based
    text: str
    words: list[Word]
    char_count: int
    needs_ocr: bool
    # text_layer: born-digital text; embedded_ocr: a scanned page image carrying someone else's
    # (invisible) OCR text, whose characters may be misread; ocr: our own OCR; none: no text
    method: str
    ocr_confidence: float | None = None  # mean Tesseract word confidence for method == "ocr"


@dataclass
class PdfContent:
    page_count: int = 0
    pages: list[PageData] = field(default_factory=list)
    error: str | None = None  # set when the file could not be read at all
    truncated: bool = False  # more pages than MAX_PAGES
    ocr_used: bool = False
    ocr_engine: str | None = None  # e.g. "tesseract 5.4.0" when OCR is available
    # Reads ONE page with our own OCR and returns its words (used to cross-check the embedded OCR
    # text of scanned filings). None when Tesseract is not installed.
    ocr_page: Callable[[int], list[Word]] | None = None


FULL_PAGE_IMAGE = 0.8  # an image covering this share of the page is a scan
INVISIBLE_TEXT = 0.8  # this share of text spans drawn invisibly (render mode 3) is an OCR layer


def has_embedded_ocr(page) -> bool:
    """A scanned page whose text comes from an invisible OCR layer (exchange filings often are).

    Detected from the page itself: a near full-page image plus text drawn invisibly over it.
    Such text can contain misread characters, so values from these pages are held to a stricter
    promotion rule.
    """
    area = abs(page.rect) or 1.0
    cover = max(
        (abs(_rect(info["bbox"]) & page.rect) / area for info in page.get_image_info()), default=0.0
    )
    if cover < FULL_PAGE_IMAGE:
        return False
    spans = page.get_texttrace()
    if not spans:
        return False
    invisible = sum(1 for span in spans if span.get("type") == 3)
    return invisible / len(spans) >= INVISIBLE_TEXT


def _rect(bbox):
    import pymupdf  # noqa: PLC0415

    return pymupdf.Rect(bbox)


def first_pages_text(source: "Path | bytes", count: int = 6) -> list[str]:
    """Text of the first few pages (for issuer checks). Empty list if the file cannot be read."""
    try:
        import pymupdf  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return []
    try:
        doc = (
            pymupdf.open(stream=source, filetype="pdf")
            if isinstance(source, bytes)
            else pymupdf.open(source, filetype="pdf")
        )
    except Exception:  # noqa: BLE001 - an unreadable file simply has no text
        return []
    with doc:
        if doc.needs_pass:
            return []
        try:
            return [
                unicodedata.normalize("NFKC", doc.load_page(i).get_text("text") or "")
                for i in range(min(count, doc.page_count))
            ]
        except Exception:  # noqa: BLE001
            return []


def page_count(source: "Path | bytes") -> int | None:
    """Number of pages, or None if the file cannot be read."""
    try:
        import pymupdf  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return None
    try:
        doc = (
            pymupdf.open(stream=source, filetype="pdf")
            if isinstance(source, bytes)
            else pymupdf.open(source, filetype="pdf")
        )
    except Exception:  # noqa: BLE001 - an unreadable file simply has no page count
        return None
    with doc:
        return None if doc.needs_pass else doc.page_count


def ocr_available() -> bool:
    """True if Tesseract is installed (see ocr.find_tesseract)."""
    return find_tesseract() is not None


def _readable_share(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 1.0
    normal = sum(1 for c in chars if c.isascii() or c in "₹–—’‘“”−")
    return normal / len(chars)


_NUMERIC_TOKEN = re.compile(r"^\(?-?[\d,]+(\.\d+)?\)?$")
UNIT_CHECK_MIN_ROWS = 8  # a page with this many rows of figures is a statement page


def lacks_unit_statement(raw_words, text: str) -> bool:
    """A page of statement figures whose text layer does not say what unit they are in.

    Some filings draw the "(Rs. in million)" line as outlines, so only OCR can read it. Counted per
    printed row, so pages with a few figures (covering letters, prose) are never touched.
    """
    from app.docintel.detect import find_units  # local: detect imports nothing from here

    if find_units(text):
        return False
    boxes = [
        Word(float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4]))
        for w in raw_words
        if str(w[4]).strip()
    ]
    rows = sum(
        1
        for line in group_lines(boxes)
        if sum(1 for w in line.words if _NUMERIC_TOKEN.match(w.text.strip())) >= 2
    )
    return rows >= UNIT_CHECK_MIN_ROWS


def merge_ocr_labels(raw_words, ocr_words):
    """Text-layer words (exact figures) plus the OCR'd WORDS that the text layer lacks.

    The figures stay exactly as the filing encodes them; only alphabetic words (row labels, the
    unit line) are taken from OCR, and only where no text-layer word already sits on that spot.
    OCR digits are never used: Tesseract mis-reads them far more often than it mis-reads words.
    """
    merged = [tuple(w[:5]) for w in raw_words]

    def covered(o) -> bool:
        cx, cy = (o.x0 + o.x1) / 2, (o.y0 + o.y1) / 2
        return any(w[0] <= cx <= w[2] and w[1] <= cy <= w[3] for w in merged[: len(raw_words)])

    added = 0
    for o in ocr_words:
        if sum(c.isalpha() for c in o.text) >= 2 and not covered(o):
            merged.append((o.x0, o.y0, o.x1, o.y1, o.text))
            added += 1
    return merged, added


POOR_EMBEDDED_OCR_SHARE = 0.03  # malformed words in a scan's own OCR text: re-read it ourselves
REPLACE_EMBEDDED_MIN_CONFIDENCE = 75.0
GOOD_OCR_CONFIDENCE = 70.0


def malformed_share(text: str) -> float:
    """Share of 4+ letter words that look like OCR debris ("llJnauditedl", a capital mid-word)."""
    words = re.findall(r"[A-Za-z]{4,}", text)
    odd = sum(bool(re.search(r"[a-z][A-Z]", w) or re.match(r"^[lI]{2}", w)) for w in words)
    return odd / len(words) if words else 0.0


def ocr_best(page) -> list:
    """OCR one page; when the default reading is poor, also try denser / automatic-layout readings.

    The default (300 dpi, one text block) reads ruled bank tables badly: a real SBI scan came back
    at 48% confidence with 87 intact amounts, against 81-84% and about 250 amounts with automatic
    layout (300 dpi) or block layouts (450 dpi). The most confident reading wins; nothing is ever
    edited. (Choosing by recognised heading words instead was tried and extracted less.)
    """
    first = ocr_png(render_png(page))
    best, best_conf = first, mean_confidence(first) or 0.0
    if best_conf >= GOOD_OCR_CONFIDENCE:
        return best
    for dpi, psm in ((450, 4), (450, 11), (300, 3)):
        try:
            alt = ocr_png(render_png(page, dpi), dpi=dpi, psm=psm)
        except OcrError:
            continue
        conf = mean_confidence(alt) or 0.0
        if alt and conf > best_conf:
            best, best_conf = alt, conf
    return best


def read_pdf(path: Path, *, use_ocr: bool | None = None) -> PdfContent:
    """Text and words for every page. Never raises for a bad file: `error` explains instead."""
    try:
        import pymupdf  # noqa: PLC0415 - optional heavy dependency, imported on use
    except ImportError:  # pragma: no cover - depends on the installation
        return PdfContent(error="PyMuPDF is not installed (pip install pymupdf)")

    ocr = ocr_available() if use_ocr is None else (use_ocr and ocr_available())
    out = PdfContent()
    if ocr_available():
        out.ocr_engine = tesseract_version()

        def ocr_page(number: int) -> list[Word]:
            with pymupdf.open(path, filetype="pdf") as again:
                return ocr_png(render_png(again.load_page(number - 1)))

        out.ocr_page = ocr_page
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - any parser failure is a property of the file
        return PdfContent(error=f"not a readable PDF ({type(exc).__name__})")
    with doc:
        if doc.needs_pass:
            return PdfContent(page_count=doc.page_count, error="password-protected PDF")
        out.page_count = doc.page_count
        out.truncated = doc.page_count > MAX_PAGES
        for index in range(min(doc.page_count, MAX_PAGES)):
            try:
                page = doc.load_page(index)
                out.pages.append(_read_page(page, index + 1, ocr, out))
            except Exception as exc:  # noqa: BLE001 - one broken page must not stop the rest
                out.pages.append(
                    PageData(
                        index + 1,
                        f"[page could not be read: {type(exc).__name__}]",
                        [],
                        0,
                        True,
                        "none",
                    )
                )
    return out


def _read_page(page, number: int, ocr: bool, out: PdfContent) -> PageData:
    page_ocr_words = None
    text = page.get_text("text") or ""
    raw_words = page.get_text("words") or []
    char_count = len(text.strip())
    scanned = char_count < MIN_TEXT_CHARS and bool(page.get_images(full=False))
    garbled = char_count >= MIN_TEXT_CHARS and _readable_share(text) < MIN_READABLE_SHARE
    method = "text_layer" if char_count else "none"
    if char_count and has_embedded_ocr(page):
        method = "embedded_ocr"
    confidence = None
    poor_embedded = (
        method == "embedded_ocr" and ocr and malformed_share(text) >= POOR_EMBEDDED_OCR_SHARE
    )
    if (scanned or garbled or poor_embedded) and ocr:
        try:
            ocr_words = ocr_best(page)
            conf = mean_confidence(ocr_words) or 0.0
            # an embedded layer is only replaced by our own reading when that is clearly good
            if ocr_words and (not poor_embedded or conf >= REPLACE_EMBEDDED_MIN_CONFIDENCE):
                raw_words = [(w.x0, w.y0, w.x1, w.y1, w.text) for w in ocr_words]
                text = "\n".join(line.text for line in group_lines(ocr_words))
                char_count = len(text.strip())
                confidence = conf
                method = "ocr"
                out.ocr_used = True
                scanned = garbled = False
                page_ocr_words = ocr_words
        except OcrError:
            pass  # the page stays flagged as unreadable; nothing is guessed
    if method == "text_layer" and ocr and not (scanned or garbled):
        if lacks_unit_statement(raw_words, text):
            try:
                # only the OCR'd line(s) that state a unit are added; no OCR digit is ever used
                from app.docintel.detect import find_units

                unit_lines = [
                    ln for ln in group_lines(ocr_png(render_png(page))) if find_units(ln.text, True)
                ]
                merged, added = merge_ocr_labels(
                    raw_words, [w for ln in unit_lines for w in ln.words]
                )
                if added:
                    raw_words = merged
                    boxes = [Word(*map(float, w[:4]), str(w[4])) for w in merged]
                    text = "\n".join(line.text for line in group_lines(boxes))
                    char_count = len(text.strip())
                    method = "text_layer+ocr"
                    out.ocr_used = True
            except OcrError:
                pass  # the unit stays unstated; the uploader is asked, nothing is guessed
    # NFKC turns typographic ligatures back into letters ("ﬂow" -> "flow", "Proﬁt" -> "Profit");
    # the rupee sign and dashes are unaffected.
    words = [
        Word(
            float(w[0]),
            float(w[1]),
            float(w[2]),
            float(w[3]),
            unicodedata.normalize("NFKC", str(w[4])),
        )
        for w in raw_words[:MAX_WORDS_PER_PAGE]
        if str(w[4]).strip()
    ]
    text = unicodedata.normalize("NFKC", text)
    if page_ocr_words is not None:  # keep the confidences: OCR words are not re-wrapped
        words = list(page_ocr_words)
    return PageData(number, text, words, char_count, scanned or garbled, method, confidence)


_VOCAB = frozenset(
    "quarter ended audited unaudited total income profit tax expenses revenue interest particulars "
    "consolidated standalone year capital assets liabilities cash equity net other bank limited "
    "segment results operations before after share per finance items".split()
)


def text_layer_quality(source: "Path | bytes", sample: int = 4) -> dict:
    """How trustworthy the embedded text of the first pages looks.

    A scan with a poor OCR layer ("Quaner ended", "llJnauditedl") has images on its pages and a
    noticeable share of malformed words (measured: 8% on a real scanned bank filing, 0% on clean
    text). Used only to explain a failure better; it never changes what is extracted.
    """
    import re as _re

    import pymupdf  # noqa: PLC0415

    doc = (
        pymupdf.open(stream=source, filetype="pdf")
        if isinstance(source, bytes)
        else pymupdf.open(source)
    )
    words = hits = images = odd = 0
    n = min(sample, len(doc))
    for i in range(n):
        page = doc[i]
        images += 1 if page.get_images() else 0
        for w in _re.findall(r"[A-Za-z]{4,}", page.get_text()):
            words += 1
            hits += w.lower() in _VOCAB
            # OCR debris: a capital inside a word ("llJnauditedl") or a leading "ll"/"Il"
            odd += bool(_re.search(r"[a-z][A-Z]", w) or _re.match(r"^[lI]{2}", w))
    doc.close()
    return {
        "pages_with_images": images,
        "pages_sampled": n,
        "vocabulary_share": round(hits / words, 3) if words else 0.0,
        "malformed_share": round(odd / words, 3) if words else 0.0,
        "words": words,
    }


SELFTEST_FIGURES = ("1,234.56", "98,765.43")


def ocr_selftest() -> dict:
    """Prove that scanned pages can be read here, end to end, with a page we make ourselves.

    Draws two statement lines, turns the page into a picture inside a PDF that has NO text layer
    (what a scan is), then runs the same render-and-recognise path real scans take. Returns what
    was found; nothing is stored. Used by `docintel.cli ocr-check` after a deployment.
    """
    import pymupdf  # noqa: PLC0415

    version = tesseract_version()
    report: dict = {"engine": version, "available": version is not None}
    if version is None:
        return {**report, "ok": False, "reason": "tesseract is not installed or not on PATH"}
    text = f"Revenue from operations {SELFTEST_FIGURES[0]}   Total assets {SELFTEST_FIGURES[1]}"
    source = pymupdf.open()
    source.new_page(width=595, height=200).insert_text((30, 100), text, fontsize=16)
    png = source.load_page(0).get_pixmap(dpi=200).tobytes("png")
    scan = pymupdf.open()
    scan.new_page(width=595, height=200).insert_image(pymupdf.Rect(0, 0, 595, 200), stream=png)
    reopened = pymupdf.open(stream=scan.tobytes(), filetype="pdf")
    page = reopened.load_page(0)
    report["text_layer_chars"] = len(page.get_text().strip())  # a scan has none
    words = ocr_png(render_png(page))
    recognised = " ".join(w.text for w in words)
    report["recognised"] = recognised
    report["figures_found"] = [f for f in SELFTEST_FIGURES if f in recognised]
    report["mean_confidence"] = mean_confidence(words)
    report["ok"] = report["text_layer_chars"] == 0 and len(report["figures_found"]) == len(
        SELFTEST_FIGURES
    )
    return report
