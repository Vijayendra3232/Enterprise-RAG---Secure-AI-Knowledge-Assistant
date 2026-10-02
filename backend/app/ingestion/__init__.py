# Ingestion package — public surface
from app.ingestion.loaders import get_loader, SUPPORTED_EXTENSIONS
from app.ingestion.chunker import create_chunks
from app.ingestion.metadata import generate_document_id, build_base_metadata
from app.ingestion.pipeline import ingest_document

__all__ = [
    "get_loader",
    "SUPPORTED_EXTENSIONS",
    "create_chunks",
    "generate_document_id",
    "build_base_metadata",
    "ingest_document",
]
