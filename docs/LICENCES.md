# Licences and the PDF library decision

This is an engineering record, not legal advice. If EquityOS is ever offered to other people, have
the licence position checked by someone qualified.

## 1. Dependency audit (2026-11-08)

Read from the installed packages' own metadata (`backend/requirements.txt`):

| Package | Licence | Matters? |
|---|---|---|
| fastapi, pydantic, pydantic-settings, SQLAlchemy, alembic, rapidfuzz, beautifulsoup4 | MIT | no |
| httpx, uvicorn | BSD-3-Clause | no |
| feedparser | BSD-2-Clause | no |
| psycopg | LGPL-3.0 | no: used unmodified as a library, which LGPL allows |
| **pymupdf** | **GNU AGPL-3.0, or a paid Artifex commercial licence** | **yes** |
| Tesseract (Docker image) | Apache-2.0 | no |
| pdfminer.six, pypdfium2 (evaluated, installed only to compare) | MIT; Apache-2.0 / BSD-3 | no |

The frontend's npm dependencies were not audited in this pass (Next.js, React are MIT); do that
before a public release.

## 2. What the AGPL means here

PyMuPDF's AGPL-3.0 is a strong copyleft licence. Its obligations apply when the combined program is
**distributed** to others, or when **others interact with it over a network** (AGPL section 13): the
operator must then offer them the complete source of the whole application under the AGPL.

* Today EquityOS is a private repository run locally and in your own Docker. Nobody else receives it
  or uses it over a network, so, as I understand it, no obligation is triggered.
* It becomes relevant the moment any of these happens: a public URL where others can use the app, a
  shared Docker image or download, a client or employer using it, or open-sourcing under a licence
  that is not AGPL-compatible.

## 3. Options when that moment comes

| Option | Cost | Notes |
|---|---|---|
| A. Replace PyMuPDF with pdfminer.six (MIT) for text and positions and pypdfium2 (Apache/BSD) for page images | One file (`app/docintel/pdf.py`, 217 lines) plus re-validation | Measured on the same real pages: pdfminer.six reproduced 880 of 904 ICICI results-statement values and 286 of 286 annual-report values identically, with **0 wrong values** and 24 missing (withheld, not guessed); about 6x slower. pypdfium2 alone was weaker for text positions (808/904, 93/286), so it is for rendering only. Embedded-OCR detection must be rewritten (font names, text render mode) |
| B. Publish EquityOS itself under AGPL-3.0 | None technically | Everyone may read and reuse the source; compatible with a portfolio project, not with keeping it closed |
| C. Buy Artifex's commercial licence | Money | Not within the Rs 0 budget |

## 4. Decision

1. **Keep PyMuPDF while EquityOS stays private.** It is the most accurate extractor measured, and
   nothing is triggered by private use.
2. **Do not put EquityOS in front of anyone else, or share its image, until option A is done**
   (or option B is chosen on purpose). This is a gate for Phase 12 deployment, recorded in
   PROJECT_STATUS.
3. **The report generator (Phase 11) must not use PyMuPDF.** Use a permissively licensed library
   (ReportLab is BSD; WeasyPrint is BSD) so that producing the PDF never adds to the problem.
4. **Keep the switch cheap:** PyMuPDF is imported in exactly one module, `app/docintel/pdf.py`. No
   other module may import it (a test enforces this), so option A stays a one-file change.
5. Re-run the licence audit when dependencies change and before any public release.
