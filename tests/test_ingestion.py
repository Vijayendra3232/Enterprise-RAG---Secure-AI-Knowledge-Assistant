"""
test_ingestion.py — Unit tests for the multi-format document ingestion pipeline.

Tests cover:
    - TXT loading
    - Markdown loading
    - CSV loading
    - XLSX loading
    - JSON loading
    - PDF loading    (requires fpdf2; skipped automatically if absent)
    - DOCX loading   (requires python-docx; skipped automatically if absent)
    - Metadata generation (all mandatory fields present)
    - Chunking       (large content is split; metadata survives splitting)
    - Document ID stability (same content → same ID regardless of filename)
    - Document ID uniqueness (different content → different ID)
    - Unsupported file type
    - Empty file
    - Invalid JSON
    - API upload endpoint (health + upload validation — no vector DB required)

All test fixtures are created programmatically in a temp directory and
cleaned up automatically after each test.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

# ── Make the backend package importable ─────────────────────────────────────
_BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, os.path.abspath(_BACKEND))

from app.ingestion.pipeline import ingest_document, SUPPORTED_EXTENSIONS
from app.ingestion.metadata import generate_document_id, build_base_metadata
from app.ingestion.chunker import create_chunks
from langchain_core.documents import Document


# ── Helpers ──────────────────────────────────────────────────────────────────

def _write(directory: str, filename: str, content: str) -> str:
    path = os.path.join(directory, filename)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def _write_bytes(directory: str, filename: str, content: bytes) -> str:
    path = os.path.join(directory, filename)
    with open(path, "wb") as fh:
        fh.write(content)
    return path


# ── Base test class ───────────────────────────────────────────────────────────

class BaseIngestionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rag_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. TXT / Markdown
# ═══════════════════════════════════════════════════════════════════════════════

class TestTXTLoading(BaseIngestionTest):
    def test_basic_txt(self):
        path = _write(self.tmp, "sample.txt", "Hello world! This is plain text.")
        chunks = ingest_document(path)
        self.assertGreater(len(chunks), 0)
        combined = " ".join(c.page_content for c in chunks)
        self.assertIn("Hello world", combined)

    def test_txt_metadata_fields(self):
        path = _write(self.tmp, "meta_test.txt", "Some content here.")
        chunks = ingest_document(path)
        meta = chunks[0].metadata
        required = ["document_id", "filename", "file_type", "source",
                    "chunk_id", "ingestion_timestamp", "document_version"]
        for field in required:
            self.assertIn(field, meta, f"Missing metadata field: {field}")
        self.assertEqual(meta["filename"], "meta_test.txt")
        self.assertEqual(meta["file_type"], "txt")


class TestMarkdownLoading(BaseIngestionTest):
    def test_basic_md(self):
        path = _write(
            self.tmp, "readme.md",
            "# Title\n\nThis is **bold** and _italic_.\n\n## Section\n\nMore text."
        )
        chunks = ingest_document(path)
        self.assertGreater(len(chunks), 0)
        combined = " ".join(c.page_content for c in chunks)
        self.assertIn("Title", combined)

    def test_md_file_type_in_metadata(self):
        path = _write(self.tmp, "doc.md", "# Doc\n\nContent.")
        chunks = ingest_document(path)
        self.assertEqual(chunks[0].metadata["file_type"], "md")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. CSV
# ═══════════════════════════════════════════════════════════════════════════════

class TestCSVLoading(BaseIngestionTest):
    _CSV = "Name,Age,Department\nAlice,30,Engineering\nBob,25,Design\nCarol,35,Product\n"

    def test_row_count(self):
        path = _write(self.tmp, "employees.csv", self._CSV)
        chunks = ingest_document(path)
        # 3 data rows → 3 documents (CSV is row-level, no extra chunking needed)
        self.assertEqual(len(chunks), 3)

    def test_row_content(self):
        path = _write(self.tmp, "employees.csv", self._CSV)
        chunks = ingest_document(path)
        combined = " ".join(c.page_content for c in chunks)
        self.assertIn("Alice", combined)
        self.assertIn("Engineering", combined)

    def test_column_names_in_metadata(self):
        path = _write(self.tmp, "employees.csv", self._CSV)
        chunks = ingest_document(path)
        self.assertIn("columns", chunks[0].metadata)
        self.assertIn("Name", chunks[0].metadata["columns"])

    def test_row_number_in_metadata(self):
        path = _write(self.tmp, "employees.csv", self._CSV)
        chunks = ingest_document(path)
        rows = [c.metadata["row"] for c in chunks]
        self.assertEqual(rows, [1, 2, 3])


# ═══════════════════════════════════════════════════════════════════════════════
# 3. XLSX
# ═══════════════════════════════════════════════════════════════════════════════

class TestXLSXLoading(BaseIngestionTest):
    def _make_xlsx(self, name="data.xlsx") -> str:
        import openpyxl
        path = os.path.join(self.tmp, name)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Scores"
        ws.append(["Student", "Score", "Grade"])
        ws.append(["Alice", 95, "A"])
        ws.append(["Bob", 82, "B"])
        ws.append(["Carol", 76, "C"])
        wb.save(path)
        return path

    def test_row_documents_produced(self):
        path = self._make_xlsx()
        chunks = ingest_document(path)
        self.assertGreater(len(chunks), 0)

    def test_sheet_name_preserved(self):
        path = self._make_xlsx()
        chunks = ingest_document(path)
        for chunk in chunks:
            self.assertEqual(chunk.metadata.get("sheet"), "Scores")

    def test_content_readable(self):
        path = self._make_xlsx()
        chunks = ingest_document(path)
        combined = " ".join(c.page_content for c in chunks)
        self.assertIn("Alice", combined)
        self.assertIn("Score", combined)

    def test_multi_sheet(self):
        import openpyxl
        path = os.path.join(self.tmp, "multi.xlsx")
        wb = openpyxl.Workbook()
        ws1 = wb.active; ws1.title = "Sheet1"
        ws1.append(["A", "B"]); ws1.append([1, 2])
        ws2 = wb.create_sheet("Sheet2")
        ws2.append(["X", "Y"]); ws2.append([10, 20])
        wb.save(path)
        chunks = ingest_document(path)
        sheets = {c.metadata["sheet"] for c in chunks}
        self.assertIn("Sheet1", sheets)
        self.assertIn("Sheet2", sheets)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. JSON
# ═══════════════════════════════════════════════════════════════════════════════

class TestJSONLoading(BaseIngestionTest):
    def test_array_of_objects(self):
        data = [
            {"name": "Alice", "role": "Engineer", "level": 3},
            {"name": "Bob",   "role": "Designer",  "level": 2},
        ]
        path = _write(self.tmp, "users.json", json.dumps(data))
        chunks = ingest_document(path)
        # One document per top-level array item
        self.assertEqual(len(chunks), 2)
        combined = " ".join(c.page_content for c in chunks)
        self.assertIn("Alice", combined)
        self.assertIn("Engineer", combined)

    def test_single_object(self):
        data = {"company": "Acme", "founded": 2010, "employees": 500}
        path = _write(self.tmp, "company.json", json.dumps(data))
        chunks = ingest_document(path)
        self.assertGreater(len(chunks), 0)
        combined = " ".join(c.page_content for c in chunks)
        self.assertIn("Acme", combined)

    def test_nested_json(self):
        data = {"person": {"name": "Alice", "address": {"city": "NYC", "zip": "10001"}}}
        path = _write(self.tmp, "nested.json", json.dumps(data))
        chunks = ingest_document(path)
        combined = " ".join(c.page_content for c in chunks)
        self.assertIn("Alice", combined)
        self.assertIn("NYC", combined)

    def test_json_index_in_metadata(self):
        data = [{"id": 1}, {"id": 2}, {"id": 3}]
        path = _write(self.tmp, "items.json", json.dumps(data))
        chunks = ingest_document(path)
        indices = [c.metadata.get("json_index") for c in chunks]
        self.assertEqual(indices, [0, 1, 2])

    def test_invalid_json_raises(self):
        path = _write(self.tmp, "bad.json", "{not valid json: }")
        with self.assertRaises((ValueError, Exception)):
            ingest_document(path)

    def test_empty_array_raises(self):
        path = _write(self.tmp, "empty.json", "[]")
        with self.assertRaises(ValueError):
            ingest_document(path)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. PDF  (requires fpdf2)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPDFLoading(BaseIngestionTest):
    def _make_pdf(self, text: str, name="test.pdf") -> str:
        try:
            from fpdf import FPDF
        except ImportError:
            self.skipTest("fpdf2 not installed — PDF tests skipped")
        path = os.path.join(self.tmp, name)
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.multi_cell(0, 10, text)
        pdf.output(path)
        return path

    def test_basic_pdf_loading(self):
        path = self._make_pdf("This is a test PDF document for RAG ingestion.")
        chunks = ingest_document(path)
        self.assertGreater(len(chunks), 0)
        combined = " ".join(c.page_content for c in chunks)
        self.assertIn("test PDF document", combined)

    def test_pdf_page_in_metadata(self):
        path = self._make_pdf("Single page PDF content.")
        chunks = ingest_document(path)
        self.assertIn("page", chunks[0].metadata)
        self.assertGreaterEqual(chunks[0].metadata["page"], 1)

    def test_pdf_file_type_in_metadata(self):
        path = self._make_pdf("Content.")
        chunks = ingest_document(path)
        self.assertEqual(chunks[0].metadata["file_type"], "pdf")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. DOCX  (requires python-docx)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDOCXLoading(BaseIngestionTest):
    def _make_docx(self, text: str, name="test.docx") -> str:
        try:
            from docx import Document as DocxDoc
        except ImportError:
            self.skipTest("python-docx not installed — DOCX tests skipped")
        path = os.path.join(self.tmp, name)
        doc = DocxDoc()
        doc.add_paragraph(text)
        doc.save(path)
        return path

    def test_basic_docx_loading(self):
        path = self._make_docx("This is a test DOCX document for RAG ingestion.")
        chunks = ingest_document(path)
        self.assertGreater(len(chunks), 0)
        combined = " ".join(c.page_content for c in chunks)
        self.assertIn("test DOCX document", combined)

    def test_docx_file_type_in_metadata(self):
        path = self._make_docx("Some content.")
        chunks = ingest_document(path)
        self.assertEqual(chunks[0].metadata["file_type"], "docx")

    def test_docx_multi_paragraph(self):
        try:
            from docx import Document as DocxDoc
        except ImportError:
            self.skipTest("python-docx not installed")
        path = os.path.join(self.tmp, "multi.docx")
        doc = DocxDoc()
        for i in range(5):
            doc.add_paragraph(f"Paragraph number {i + 1} with content about enterprise RAG.")
        doc.save(path)
        chunks = ingest_document(path)
        self.assertGreater(len(chunks), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Metadata
# ═══════════════════════════════════════════════════════════════════════════════

class TestMetadata(BaseIngestionTest):
    def test_all_required_fields_present(self):
        path = _write(self.tmp, "check.txt", "Field validation test.")
        chunks = ingest_document(path)
        meta = chunks[0].metadata
        required = [
            "document_id", "filename", "file_type", "source",
            "page", "chunk_id", "ingestion_timestamp", "document_version",
        ]
        for field in required:
            self.assertIn(field, meta, f"Missing metadata field: {field}")

    def test_chunk_id_format(self):
        path = _write(self.tmp, "chunk_id.txt", "Testing chunk ID assignment.")
        chunks = ingest_document(path)
        doc_id = chunks[0].metadata["document_id"]
        for i, chunk in enumerate(chunks):
            expected = f"{doc_id}_{i}"
            self.assertEqual(chunk.metadata["chunk_id"], expected)

    def test_ingestion_timestamp_is_iso(self):
        from datetime import datetime
        path = _write(self.tmp, "ts.txt", "Timestamp test.")
        chunks = ingest_document(path)
        ts = chunks[0].metadata["ingestion_timestamp"]
        # Should parse without errors
        datetime.fromisoformat(ts)

    def test_source_is_absolute_path(self):
        path = _write(self.tmp, "source.txt", "Source path test.")
        chunks = ingest_document(path)
        self.assertTrue(os.path.isabs(chunks[0].metadata["source"]))


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Document ID
# ═══════════════════════════════════════════════════════════════════════════════

class TestDocumentID(BaseIngestionTest):
    def test_same_content_same_id(self):
        """Two files with identical content must share the same document_id."""
        content = "Identical content for both files."
        p1 = _write(self.tmp, "file_alpha.txt", content)
        p2 = _write(self.tmp, "file_beta.txt", content)
        self.assertEqual(generate_document_id(p1), generate_document_id(p2))

    def test_different_content_different_id(self):
        p1 = _write(self.tmp, "a.txt", "Content A.")
        p2 = _write(self.tmp, "b.txt", "Content B.")
        self.assertNotEqual(generate_document_id(p1), generate_document_id(p2))

    def test_document_id_is_64_hex_chars(self):
        path = _write(self.tmp, "hex.txt", "SHA-256 produces 64 hex chars.")
        doc_id = generate_document_id(path)
        self.assertEqual(len(doc_id), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in doc_id))

    def test_document_id_in_pipeline_output(self):
        path = _write(self.tmp, "pipeline.txt", "Testing document ID from pipeline.")
        chunks = ingest_document(path)
        for chunk in chunks:
            self.assertIsNotNone(chunk.metadata.get("document_id"))
            self.assertEqual(len(chunk.metadata["document_id"]), 64)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Chunking
# ═══════════════════════════════════════════════════════════════════════════════

class TestChunking(BaseIngestionTest):
    def test_large_content_is_split(self):
        # ~3000 chars — must be split into multiple chunks at default chunk_size=500
        long_text = "Enterprise RAG systems process large corpora efficiently. " * 50
        path = _write(self.tmp, "long.txt", long_text)
        chunks = ingest_document(path)
        self.assertGreater(len(chunks), 1)

    def test_short_content_is_single_chunk(self):
        path = _write(self.tmp, "short.txt", "Brief document.")
        chunks = ingest_document(path)
        self.assertEqual(len(chunks), 1)

    def test_metadata_survives_chunking(self):
        long_text = "The quick brown fox jumps over the lazy dog. " * 60
        path = _write(self.tmp, "chunked.txt", long_text)
        chunks = ingest_document(path)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertIn("document_id", chunk.metadata)
            self.assertIn("chunk_id", chunk.metadata)
            self.assertIn("filename", chunk.metadata)

    def test_configurable_chunk_size(self):
        text = "Word " * 200  # 1000 chars
        docs = [Document(page_content=text, metadata={"source": "test"})]
        small_chunks = create_chunks(docs, chunk_size=100, chunk_overlap=10)
        large_chunks = create_chunks(docs, chunk_size=500, chunk_overlap=50)
        self.assertGreater(len(small_chunks), len(large_chunks))


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Error Handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestErrorHandling(BaseIngestionTest):
    def test_unsupported_file_type_raises_value_error(self):
        path = _write(self.tmp, "script.exe", "fake binary")
        with self.assertRaises(ValueError) as ctx:
            ingest_document(path)
        self.assertIn("Unsupported", str(ctx.exception))

    def test_unknown_extension_raises_value_error(self):
        path = _write(self.tmp, "file.xyz123", "content")
        with self.assertRaises(ValueError):
            ingest_document(path)

    def test_empty_txt_raises_value_error(self):
        path = _write(self.tmp, "empty.txt", "")
        with self.assertRaises(ValueError) as ctx:
            ingest_document(path)
        self.assertIn("empty", str(ctx.exception).lower())

    def test_empty_csv_raises_value_error(self):
        path = _write(self.tmp, "empty.csv", "")
        with self.assertRaises((ValueError, Exception)):
            ingest_document(path)

    def test_missing_file_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            ingest_document("/nonexistent/path/to/file.txt")

    def test_invalid_json_raises(self):
        path = _write(self.tmp, "bad.json", "{bad json: [}")
        with self.assertRaises((ValueError, Exception)):
            ingest_document(path)


# ═══════════════════════════════════════════════════════════════════════════════
# 11. API Endpoint Tests (no vector DB required)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDocumentAPIValidation(unittest.TestCase):
    """
    Test the documents API router in isolation (without triggering the full
    FastAPI lifespan / vector DB init).  We import the router directly and
    wrap it in a minimal app so we don't need dir3 or a Groq key.
    """

    @classmethod
    def setUpClass(cls):
        import sys
        sys.path.insert(0, os.path.abspath(_BACKEND))
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.auth.jwt import create_access_token
        from app.api.documents import router
        test_app = FastAPI()
        test_app.include_router(router)
        token = create_access_token({
            "sub": "admin_a",
            "tenant_id": "company_a",
            "email": "admin@companya.com",
            "name": "Admin",
            "role": "ADMIN"
        })
        cls.client = TestClient(test_app, headers={"Authorization": f"Bearer {token}"})
        cls.tmp = tempfile.mkdtemp(prefix="rag_api_test_")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_upload_txt_returns_200(self):
        content = b"Hello from the API upload test."
        response = self.client.post(
            "/documents/upload",
            files={"file": ("api_test.txt", content, "text/plain")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["file_type"], "txt")
        self.assertGreater(data["num_chunks"], 0)
        self.assertEqual(len(data["document_id"]), 64)

    def test_upload_csv_returns_200(self):
        csv_bytes = b"Name,Score\nAlice,90\nBob,85\n"
        response = self.client.post(
            "/documents/upload",
            files={"file": ("scores.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["file_type"], "csv")
        self.assertEqual(data["num_chunks"], 2)  # 2 data rows

    def test_upload_json_returns_200(self):
        payload = json.dumps([{"name": "Alice"}, {"name": "Bob"}]).encode()
        response = self.client.post(
            "/documents/upload",
            files={"file": ("users.json", payload, "application/json")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["file_type"], "json")
        self.assertEqual(data["num_chunks"], 2)

    def test_unsupported_extension_returns_415(self):
        response = self.client.post(
            "/documents/upload",
            files={"file": ("malware.exe", b"fake binary", "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 415)

    def test_empty_file_returns_400(self):
        response = self.client.post(
            "/documents/upload",
            files={"file": ("empty.txt", b"", "text/plain")},
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_json_returns_422(self):
        response = self.client.post(
            "/documents/upload",
            files={"file": ("bad.json", b"{not json}", "application/json")},
        )
        self.assertEqual(response.status_code, 422)

    def test_response_has_all_fields(self):
        content = b"Field completeness test."
        response = self.client.post(
            "/documents/upload",
            files={"file": ("fields.txt", content, "text/plain")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for field in ["document_id", "filename", "file_type", "num_chunks", "status", "message"]:
            self.assertIn(field, data, f"Missing response field: {field}")


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
