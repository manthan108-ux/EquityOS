"""ZIP kits of filings: only real PDFs come out, within limits, with safe names."""

import zipfile

import pytest

from app.docintel import archives
from app.docintel.archives import ArchiveError, read_pdf_members

PDF = b"%PDF-1.7\n%fake but starts like a PDF\n"


def make_zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def test_extracts_only_real_pdfs_with_safe_base_names(tmp_path):
    z = make_zip(
        tmp_path / "kit.zip",
        {
            "results/Q1 results.pdf": PDF,
            "../../evil/..\escape.pdf": PDF,
            "notes.txt": b"hello",
            "fake.pdf": b"not a pdf at all",
            "__MACOSX/._junk.pdf": PDF,
            ".hidden.pdf": PDF,
        },
    )
    names = sorted(n for n, _ in read_pdf_members(z))
    assert names == ["Q1 results.pdf", "escape.pdf"]
    assert all("/" not in n and "\\" not in n and ".." not in n for n in names)


def test_not_a_zip_and_too_many_members_are_refused(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"this is not a zip")
    with pytest.raises(ArchiveError, match="valid ZIP"):
        read_pdf_members(bad)
    many = make_zip(
        tmp_path / "many.zip", {f"f{i}.pdf": PDF for i in range(archives.MAX_MEMBERS + 1)}
    )
    with pytest.raises(ArchiveError, match="more than"):
        read_pdf_members(many)


def test_size_is_measured_while_reading_not_trusted_from_the_header(tmp_path, monkeypatch):
    monkeypatch.setattr(archives, "MAX_MEMBER_BYTES", 1000)
    z = make_zip(tmp_path / "big.zip", {"big.pdf": PDF + b"0" * 5000})
    with pytest.raises(ArchiveError, match="size limit"):
        read_pdf_members(z)
    monkeypatch.setattr(archives, "MAX_MEMBER_BYTES", 10_000)
    monkeypatch.setattr(archives, "MAX_TOTAL_BYTES", 6000)
    z2 = make_zip(tmp_path / "two.zip", {"a.pdf": PDF + b"0" * 4000, "b.pdf": PDF + b"0" * 4000})
    with pytest.raises(ArchiveError, match="size limit"):
        read_pdf_members(z2)
