"""Local processing utilities: ZIP extraction and invoice CSV generation.

These are our own orchestration work (not OCR): unpacking archives and producing
the bulk-invoice review CSV (Row, Invoice No., Date, Due Date, Customer, Amount,
Flag) that the SME approves.
"""

from __future__ import annotations

import csv
import io
import zipfile
from typing import Any

from .errors import ZipExtractionError

# Skip macOS/Windows archive cruft when unpacking.
_IGNORED_PREFIXES = ("__MACOSX/", ".")


def extract_zip(content: bytes) -> list[tuple[str, bytes]]:
    """Return ``(filename, bytes)`` for each real file entry in a ZIP archive."""

    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ZipExtractionError("Uploaded file is not a valid ZIP archive") from exc

    entries: list[tuple[str, bytes]] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename
        base = name.rsplit("/", 1)[-1]
        if name.startswith(_IGNORED_PREFIXES) or base.startswith(".") or not base:
            continue
        entries.append((base, archive.read(info)))
    return entries


# Columns for the bulk-invoice review CSV (matches the agentic flow spec).
INVOICE_CSV_COLUMNS = [
    "Row",
    "Invoice No.",
    "Date",
    "Due Date",
    "Customer",
    "Amount QAR",
    "Flag",
]


def generate_invoice_csv(rows: list[dict[str, Any]]) -> bytes:
    """Build the bulk-invoice review CSV from extracted invoice fields."""

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(INVOICE_CSV_COLUMNS)
    for index, fields in enumerate(rows, start=1):
        writer.writerow(
            [
                index,
                fields.get("invoice_no", ""),
                fields.get("date", ""),
                fields.get("due_date", ""),
                fields.get("customer", ""),
                fields.get("amount", ""),
                fields.get("flag", ""),
            ]
        )
    return buffer.getvalue().encode("utf-8")
