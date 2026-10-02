"""
pipeline.py — High-level document ingestion orchestrator.

Public API
----------
ingest_document(file_path) -> List[Document]

Pipeline stages
---------------
file
 ↓  detect type & validate
 ↓  generate stable document_id
 ↓  load with format-specific loader
 ↓  clean / normalize text
 ↓  chunk  (structured data: only if rows exceed CHUNK_SIZE)
 ↓  assign chunk_id to every chunk
 →  return chunks  (vector-DB insertion is handled separately)

Can also be run standalone from the command line:
    python -m app.ingestion.pipeline path/to/file.pdf
"""

import os
import re
import sys
from typing import List, Optional

from langchain_core.documents import Document

from app import config
from app.ingestion.chunker import create_chunks
from app.ingestion.loaders import SUPPORTED_EXTENSIONS, get_loader
from app.ingestion.metadata import build_base_metadata, generate_document_id

# ---------------------------------------------------------------------------
# Formats treated as already "row-level" — skip chunking unless rows are large
# ---------------------------------------------------------------------------
_STRUCTURED_FORMATS: set[str] = {"csv", "xlsx", "json"}


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def _normalize_text(text: str) -> str:
    """Remove excessive blank lines and trailing whitespace."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def _normalize_document(doc: Document) -> Document:
    doc.page_content = _normalize_text(doc.page_content)
    return doc


# ---------------------------------------------------------------------------
# Main pipeline function
# ---------------------------------------------------------------------------

def ingest_document(
    file_path: str,
    *,
    tenant_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    access_level: Optional[str] = None,
    allowed_roles: Optional[List[str]] = None,
    allowed_user_ids: Optional[List[str]] = None,
    permission_status: str = "KNOWN",
) -> List[Document]:
    """
    Run the full ingestion pipeline for a single file.

    Args:
        file_path: Absolute path to the source file.
        tenant_id: Multi-tenant identifier.
        owner_id: Document owner / uploader user ID.
        access_level: "PRIVATE", "USER", "ROLE", "TENANT", "PUBLIC".
        allowed_roles: List of roles permitted to access.
        allowed_user_ids: List of user IDs permitted to access.
        permission_status: "KNOWN", "UNKNOWN".

    Returns:
        List of LangChain Documents with rich metadata and assigned chunk_ids.
    """
    # ── 0. Basic file checks ────────────────────────────────────────────────
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    filename = os.path.basename(file_path)
    if "." not in filename:
        raise ValueError(f"File has no extension and cannot be processed: {filename}")

    ext = filename.rsplit(".", 1)[-1].lower()

    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type: .{ext}. "
            f"Supported formats: {sorted(SUPPORTED_EXTENSIONS)}"
        )

    if os.path.getsize(file_path) == 0:
        raise ValueError(f"File is empty: {filename}")

    # ── 1. Generate stable document ID (content hash) ───────────────────────
    document_id = generate_document_id(file_path)
    print(f"[Pipeline] document_id={document_id[:16]}...  file={filename}")

    # ── 2. Build base metadata ───────────────────────────────────────────────
    base_metadata = build_base_metadata(
        file_path,
        document_id,
        tenant_id=tenant_id,
        owner_id=owner_id,
        access_level=access_level,
        allowed_roles=allowed_roles,
        allowed_user_ids=allowed_user_ids,
        permission_status=permission_status
    )

    # ── 3. Load with format-specific loader ─────────────────────────────────
    loader = get_loader(ext)
    raw_docs = loader.load(file_path, base_metadata)

    if not raw_docs:
        raise ValueError(f"No content could be extracted from: {filename}")

    # ── 4. Normalize text ────────────────────────────────────────────────────
    raw_docs = [_normalize_document(doc) for doc in raw_docs]
    raw_docs = [doc for doc in raw_docs if doc.page_content.strip()]

    if not raw_docs:
        raise ValueError(
            f"Document produced no usable text after normalization: {filename}"
        )

    # ── 5. Chunk ─────────────────────────────────────────────────────────────
    if ext in _STRUCTURED_FORMATS:
        # Keep rows/items intact; only chunk further if a row is unusually large
        needs_chunking = any(len(doc.page_content) > config.CHUNK_SIZE for doc in raw_docs)
        chunks = create_chunks(raw_docs) if needs_chunking else raw_docs
    else:
        chunks = create_chunks(raw_docs)

    # ── 6. Assign chunk IDs ──────────────────────────────────────────────────
    for i, chunk in enumerate(chunks):
        chunk.metadata["chunk_id"] = f"{document_id}_{i}"

    print(f"[Pipeline] Finished: {len(chunks)} chunks from '{filename}'")
    return chunks


# ---------------------------------------------------------------------------
# Directory Ingestion Orchestrator
# ---------------------------------------------------------------------------

class IngestionPipeline:
    """
    IngestionPipeline coordinates the loading, chunking, and saving of documents
    from a source directory to a persistent vector store.
    """
    def __init__(self, embedding_model_name: str = config.EMBEDDING_MODEL_NAME):
        self.embedding_model_name = embedding_model_name

    def run(self, data_dir: str, persist_dir: str, collection_name: str):
        print(f"[IngestionPipeline] Ingesting files from {data_dir} to {persist_dir}...")
        
        all_chunks = []
        if not os.path.exists(data_dir):
            print(f"[IngestionPipeline] Data directory {data_dir} does not exist.")
            return None
            
        for file in os.listdir(data_dir):
            file_path = os.path.join(data_dir, file)
            if os.path.isdir(file_path):
                continue
            if file.startswith(".") or "." not in file:
                continue
            ext = file.rsplit(".", 1)[-1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            try:
                chunks = ingest_document(file_path)
                all_chunks.extend(chunks)
            except Exception as e:
                print(f"[IngestionPipeline] Error ingesting {file}: {e}")
                
        if not all_chunks:
            print("[IngestionPipeline] No documents ingested.")
            return None
            
        from app.core.embeddings import load_embedding_model
        import chromadb
        from langchain_community.vectorstores import Chroma
        
        embeddings = load_embedding_model(self.embedding_model_name)
        client = chromadb.PersistentClient(persist_dir)
        
        vectordb = Chroma.from_documents(
            documents=all_chunks,
            collection_name=collection_name,
            embedding=embeddings,
            persist_directory=persist_dir
        )
        print(f"[IngestionPipeline] Saved {len(all_chunks)} chunks to collection '{collection_name}'.")
        return vectordb


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m app.ingestion.pipeline <file_path_or_dir_path>")
        sys.exit(1)

    target = sys.argv[1]
    if os.path.isdir(target):
        pipeline = IngestionPipeline()
        pipeline.run(target, config.VECTOR_DB_DIR, config.COLLECTION_NAME)
    else:
        result = ingest_document(target)
        print(f"\nTotal chunks: {len(result)}")
        if result:
            print("\n--- First chunk preview ---")
            print(result[0].page_content[:300])
            print("\n--- Metadata ---")
            for k, v in result[0].metadata.items():
                print(f"  {k}: {v}")
