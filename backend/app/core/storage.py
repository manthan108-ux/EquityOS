"""One place that decides where documents live and how a stored path is resolved.

Layout (identical on your machine and in Docker, because Docker bind-mounts the same folder):

    <DATA_DIR>/documents/<aa>/<sha256>.<ext>   downloaded or uploaded files (content-addressed)
    <DATA_DIR>/cache/                           HTTP cache
    <DATA_DIR>/inbox/<SYMBOL>/                  files you drop in for manual ingestion

`DATA_DIR` is the single setting. The database stores only the path RELATIVE to it
(`documents/c4/c48d....pdf`), so the same database rows work in both environments.
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_data_dir
from app.models import Document

DOCUMENTS_SUBDIR = "documents"
INBOX_SUBDIR = "inbox"
CACHE_SUBDIR = "cache"


def data_root() -> Path:
    return resolve_data_dir(get_settings())


class StoragePathError(ValueError):
    """A stored path points outside the data directory (never followed)."""


def resolve_inside(root: Path, relative: str) -> Path:
    """`root / relative`, refused if it escapes `root` (absolute paths, `..`, symlink tricks)."""
    base = root.resolve()
    candidate = (base / relative).resolve()
    if base != candidate and base not in candidate.parents:
        raise StoragePathError(f"path escapes the data directory: {relative!r}")
    return candidate


@dataclass
class StorageReport:
    root: str
    exists: bool
    writable: bool
    documents: int = 0
    missing_files: list[str] = field(default_factory=list)  # document ids whose file is absent
    hash_mismatches: list[str] = field(default_factory=list)  # present but content differs
    unreferenced_files: int = 0  # files under documents/ that no database row points to
    verified_hashes: bool = False

    @property
    def healthy(self) -> bool:
        return self.exists and self.writable and not self.missing_files and not self.hash_mismatches


def _writable(root: Path) -> bool:
    try:
        probe = root / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def storage_report(
    session: Session, root: Path | None = None, *, verify_hashes: bool = False
) -> StorageReport:
    """Do the database rows and the files on disk agree? Read-only apart from a tiny write probe."""
    root = root or data_root()
    report = StorageReport(
        root=str(root), exists=root.is_dir(), writable=root.is_dir() and _writable(root)
    )
    referenced: set[Path] = set()
    for doc in session.scalars(select(Document)):
        report.documents += 1
        try:
            path = resolve_inside(root, doc.storage_path)
        except StoragePathError:
            report.missing_files.append(str(doc.id))
            continue
        referenced.add(path)
        if not path.is_file():
            report.missing_files.append(str(doc.id))
        elif verify_hashes:
            report.verified_hashes = True
            if hashlib.sha256(path.read_bytes()).hexdigest() != doc.sha256:
                report.hash_mismatches.append(str(doc.id))
    docs_dir = root / DOCUMENTS_SUBDIR
    if docs_dir.is_dir():
        report.unreferenced_files = sum(
            1 for p in docs_dir.rglob("*") if p.is_file() and p.resolve() not in referenced
        )
    return report
