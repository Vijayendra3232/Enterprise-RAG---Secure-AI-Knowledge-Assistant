"""
loaders.py — Format-specific document loaders with a unified abstract interface.

Each loader receives a file path and a pre-built metadata dict, and returns
a list of LangChain Documents.  The factory function `get_loader(ext)` selects
the right loader automatically.

Supported formats:
    .pdf   — PyPDFLoader (one Document per page, page number preserved)
    .docx  — Docx2txtLoader  (full document as one Document)
    .txt   — Plain text reader
    .md    — Plain text reader (Markdown preserved as-is)
    .csv   — pandas row-by-row (one Document per row)
    .xlsx  — pandas sheet+row-by-row (one Document per row, sheet name preserved)
    .json  — Recursive flatten (one Document per top-level item in an array,
              or one Document for a single object)
"""

import json
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List

import pandas as pd
from langchain_core.documents import Document
from langchain_community.document_loaders import PyPDFLoader, Docx2txtLoader


# ---------------------------------------------------------------------------
# Abstract Base
# ---------------------------------------------------------------------------

class BaseLoader(ABC):
    """All loaders must implement `load()` and return LangChain Documents."""

    @abstractmethod
    def load(self, file_path: str, metadata: Dict[str, Any]) -> List[Document]:
        """
        Load a file and return a list of Documents.

        Args:
            file_path: Absolute path to the file.
            metadata:  Pre-built base metadata dict (will be shallow-copied
                       and extended per document / page / row as needed).

        Returns:
            Non-empty list of LangChain Document objects.

        Raises:
            ValueError: If the file is empty or unreadable.
        """


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

class PDFFileLoader(BaseLoader):
    """Loads PDF files via PyPDFLoader, one Document per page."""

    def load(self, file_path: str, metadata: Dict[str, Any]) -> List[Document]:
        loader = PyPDFLoader(file_path)
        raw_pages = loader.load_and_split()

        docs: List[Document] = []
        for raw in raw_pages:
            page_num = raw.metadata.get("page", 0) + 1  # PyPDF is 0-indexed
            page_meta = {
                **metadata,
                "page": page_num,
                "file_path": file_path,
            }
            docs.append(Document(page_content=raw.page_content, metadata=page_meta))

        if not docs:
            raise ValueError(f"PDF produced no pages: {os.path.basename(file_path)}")
        return docs


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

class DOCXFileLoader(BaseLoader):
    """Loads .docx files via Docx2txtLoader (full document, page = 1)."""

    def load(self, file_path: str, metadata: Dict[str, Any]) -> List[Document]:
        loader = Docx2txtLoader(file_path)
        raw_docs = loader.load()

        if not raw_docs or not raw_docs[0].page_content.strip():
            raise ValueError(f"DOCX produced no content: {os.path.basename(file_path)}")

        doc_meta = {**metadata, "page": 1, "file_path": file_path}
        return [Document(page_content=raw_docs[0].page_content, metadata=doc_meta)]


# ---------------------------------------------------------------------------
# TXT
# ---------------------------------------------------------------------------

class TXTFileLoader(BaseLoader):
    """Loads plain text files."""

    def load(self, file_path: str, metadata: Dict[str, Any]) -> List[Document]:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()

        if not content.strip():
            raise ValueError(f"Text file is empty: {os.path.basename(file_path)}")

        doc_meta = {**metadata, "page": 1, "file_path": file_path}
        return [Document(page_content=content, metadata=doc_meta)]


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

class MarkdownFileLoader(BaseLoader):
    """Loads Markdown files as plain text (structure preserved as-is)."""

    def load(self, file_path: str, metadata: Dict[str, Any]) -> List[Document]:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()

        if not content.strip():
            raise ValueError(f"Markdown file is empty: {os.path.basename(file_path)}")

        doc_meta = {**metadata, "page": 1, "file_path": file_path}
        return [Document(page_content=content, metadata=doc_meta)]


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

class CSVFileLoader(BaseLoader):
    """
    Loads CSV files row-by-row.

    Each row becomes its own Document, formatted as:
        "Row N | col1: val1 | col2: val2 | ..."

    This preserves column semantics and allows fine-grained retrieval
    without collapsing the entire spreadsheet into a single blob.
    """

    def load(self, file_path: str, metadata: Dict[str, Any]) -> List[Document]:
        try:
            df = pd.read_csv(file_path, dtype=str)
        except Exception as exc:
            raise ValueError(f"Could not read CSV '{os.path.basename(file_path)}': {exc}") from exc

        if df.empty:
            raise ValueError(f"CSV file is empty: {os.path.basename(file_path)}")

        columns = list(df.columns)
        docs: List[Document] = []

        for idx, row in df.iterrows():
            row_text = f"Row {idx + 1} | " + " | ".join(
                f"{col}: {val}" for col, val in zip(columns, row)
            )
            row_meta = {
                **metadata,
                "page": 1,
                "file_path": file_path,
                "row": int(idx) + 1,
                "columns": ", ".join(columns),
            }
            docs.append(Document(page_content=row_text, metadata=row_meta))

        return docs


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------

class XLSXFileLoader(BaseLoader):
    """
    Loads Excel files sheet-by-sheet, row-by-row.

    Each row becomes its own Document, formatted as:
        "Sheet: <name> | Row N | col1: val1 | col2: val2 | ..."

    Sheet name, row number and column names are all preserved in metadata.
    """

    def load(self, file_path: str, metadata: Dict[str, Any]) -> List[Document]:
        try:
            xl = pd.ExcelFile(file_path, engine="openpyxl")
        except Exception as exc:
            raise ValueError(
                f"Could not open Excel file '{os.path.basename(file_path)}': {exc}"
            ) from exc

        docs: List[Document] = []

        for sheet_name in xl.sheet_names:
            try:
                df = xl.parse(sheet_name, dtype=str)
            except Exception as exc:
                raise ValueError(
                    f"Could not read sheet '{sheet_name}' in "
                    f"'{os.path.basename(file_path)}': {exc}"
                ) from exc

            if df.empty:
                continue

            columns = list(df.columns)
            for idx, row in df.iterrows():
                row_text = (
                    f"Sheet: {sheet_name} | Row {idx + 1} | "
                    + " | ".join(f"{col}: {val}" for col, val in zip(columns, row))
                )
                row_meta = {
                    **metadata,
                    "page": 1,
                    "file_path": file_path,
                    "sheet": sheet_name,
                    "row": int(idx) + 1,
                    "columns": ", ".join(columns),
                }
                docs.append(Document(page_content=row_text, metadata=row_meta))

        if not docs:
            raise ValueError(f"Excel file produced no rows: {os.path.basename(file_path)}")

        return docs


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

class JSONFileLoader(BaseLoader):
    """
    Loads JSON files with support for:
      - Arrays of objects  → one Document per item
      - Single objects     → one Document (recursively flattened)
      - Nested structures  → recursively expanded to key: value lines

    Raises ValueError on invalid JSON or empty content.
    """

    _MAX_DEPTH = 15

    def load(self, file_path: str, metadata: Dict[str, Any]) -> List[Document]:
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON in '{os.path.basename(file_path)}': {exc}"
            ) from exc

        docs: List[Document] = []

        if isinstance(data, list):
            if not data:
                raise ValueError(f"JSON array is empty: {os.path.basename(file_path)}")
            for idx, item in enumerate(data):
                text = self._flatten(item)
                item_meta = {
                    **metadata,
                    "page": 1,
                    "file_path": file_path,
                    "json_index": idx,
                }
                docs.append(Document(page_content=text, metadata=item_meta))
        elif isinstance(data, dict):
            if not data:
                raise ValueError(f"JSON object is empty: {os.path.basename(file_path)}")
            text = self._flatten(data)
            doc_meta = {**metadata, "page": 1, "file_path": file_path}
            docs.append(Document(page_content=text, metadata=doc_meta))
        else:
            # Scalar root value
            text = str(data)
            doc_meta = {**metadata, "page": 1, "file_path": file_path}
            docs.append(Document(page_content=text, metadata=doc_meta))

        return docs

    def _flatten(self, data: Any, prefix: str = "", depth: int = 0) -> str:
        """Recursively convert nested JSON to readable key: value lines."""
        if depth > self._MAX_DEPTH:
            return f"{prefix}: {data!r}"

        parts: List[str] = []

        if isinstance(data, dict):
            for key, val in data.items():
                full_key = f"{prefix}.{key}" if prefix else str(key)
                if isinstance(val, (dict, list)):
                    parts.append(self._flatten(val, full_key, depth + 1))
                else:
                    parts.append(f"{full_key}: {val}")
        elif isinstance(data, list):
            for i, item in enumerate(data):
                parts.append(self._flatten(item, f"{prefix}[{i}]", depth + 1))
        else:
            parts.append(f"{prefix}: {data}" if prefix else str(data))

        return "\n".join(p for p in parts if p.strip())


# ---------------------------------------------------------------------------
# Registry & Factory
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS: set[str] = {"pdf", "docx", "txt", "md", "csv", "xlsx", "json"}

_LOADER_MAP: Dict[str, type] = {
    "pdf":  PDFFileLoader,
    "docx": DOCXFileLoader,
    "txt":  TXTFileLoader,
    "md":   MarkdownFileLoader,
    "csv":  CSVFileLoader,
    "xlsx": XLSXFileLoader,
    "json": JSONFileLoader,
}


def get_loader(extension: str) -> BaseLoader:
    """
    Return the correct loader instance for a file extension.

    Args:
        extension: Lowercase extension without the dot (e.g. "pdf").

    Raises:
        ValueError: If the extension is not supported.
    """
    if extension not in _LOADER_MAP:
        raise ValueError(
            f"Unsupported file type: .{extension}. "
            f"Supported: {sorted(SUPPORTED_EXTENSIONS)}"
        )
    return _LOADER_MAP[extension]()
