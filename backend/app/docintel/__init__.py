"""Document intelligence (Phase 5): read official PDFs and extract validated financial statements.

Modules:
  pdf        page text and word positions (PyMuPDF; OCR only if Tesseract is installed)
  layout     words -> lines -> numeric columns -> table rows
  detect     statement titles, reporting units, consolidated/standalone basis
  periods    reporting period of a column from its header text
  taxonomy   printed labels -> canonical line items (Ind AS and bank formats)
  statements statement tables on a page, every cell kept with provenance
  validate   arithmetic identities per statement column
  pipeline   run on a stored document, stage everything, promote only validated values
"""
