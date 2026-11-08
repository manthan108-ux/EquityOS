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
    if (scanned or garbled) and ocr:
        try:
            ocr_words = ocr_png(render_png(page))
            if ocr_words:
                raw_words = [(w.x0, w.y0, w.x1, w.y1, w.text) for w in ocr_words]
                text = "\n".join(line.text for line in group_lines(ocr_words))
                char_count = len(text.strip())
                confidence = mean_confidence(ocr_words)
                method = "ocr"
                out.ocr_used = True
                scanned = garbled = False
                page_ocr_words = ocr_words
        except OcrError:
            pass  # the page stays flagged as unreadable; nothing is guessed
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
