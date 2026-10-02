"""
ingestion.py — Idempotent Document Ingestion Task Handler.
Orchestrates document parsing, chunking, embedding, database chunk persistence,
and OpenSearch bulk indexing.
Includes explicit version-race protection and deletion-race protection to prevent
stale workers from overwriting newer document versions or resurrecting deleted documents.
"""

import os
import json
import hashlib
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from app.tasks.handlers.base import TaskHandler, WorkerContext, RetryableTaskError, NonRetryableTaskError
from app.tasks.models import TaskType
from app.storage.models.task import Task
from app.storage.models.document import Document as DBDocument
from app.storage.models.chunk import DocumentChunk as DBDocumentChunk
from app.storage.repositories.document_repository import SQLDocumentRepository
from app.storage.repositories.permission_repository import SQLPermissionRepository
from app.storage.repositories.chunk_repository import SQLChunkRepository
from app.storage.search.models import ChunkPayload
from app.ingestion.pipeline import ingest_document
from app.security.audit import log_security_event
from app.storage.blob.errors import StorageIntegrityError

logger = logging.getLogger(__name__)


class DocumentIngestHandler(TaskHandler):
    """
    Asynchronous document ingestion worker handler.
    """

    def can_handle(self, task_type: str) -> bool:
        return task_type == TaskType.DOCUMENT_INGEST.value

    def handle(self, task: Task, context: WorkerContext) -> Dict[str, Any]:
        payload = task.payload or {}
        document_id = payload.get("document_id")
        tenant_id = task.tenant_id
        expected_version = payload.get("document_version", 1)
        storage_path = payload.get("storage_path")
        owner_id = payload.get("owner_id", "system")
        access_level = payload.get("access_level", "PRIVATE")
        allowed_roles = payload.get("allowed_roles", [])
        allowed_user_ids = payload.get("allowed_user_ids", [])

        if not document_id or not storage_path:
            raise NonRetryableTaskError(
                f"Invalid DOCUMENT_INGEST payload: missing document_id or storage_path in task {task.id}."
            )

        doc_repo = SQLDocumentRepository(context.db)
        perm_repo = SQLPermissionRepository(context.db)
        chunk_repo = SQLChunkRepository(context.db)

        # ── 1. Race Check: Document Existence, Deletion & Version ───────────
        db_doc = doc_repo.get_by_id(document_id, tenant_id=tenant_id)
        if not db_doc or db_doc.status in ["DELETING", "DELETED"]:
            logger.info(
                f"[IngestHandler] Document '{document_id}' was deleted before worker processing. Aborting safely."
            )
            return {"status": "aborted", "reason": "document_deleted", "document_id": document_id}

        if db_doc.version != expected_version:
            logger.warning(
                f"[IngestHandler] Stale worker race: DB version ({db_doc.version}) != task version ({expected_version}). Aborting."
            )
            return {
                "status": "aborted",
                "reason": "superseded_by_newer_version",
                "current_version": db_doc.version,
                "task_version": expected_version,
            }

        # ── 2. Transition DB State to PROCESSING ────────────────────────────
        updated_doc = doc_repo.update_status(document_id, status="PROCESSING")
        current_version = updated_doc.version if updated_doc else db_doc.version + 1
        context.db.commit()

        # ── 3. Parse and Chunk Document (with S3 retrieval & SHA-256 verification) ─
        from app.storage.blob import get_document_storage
        blob_storage = context.blob_storage or get_document_storage()
        orig_filename = db_doc.filename or "document.txt"
        ext = orig_filename.rsplit(".", 1)[-1].lower() if "." in orig_filename else "txt"

        try:
            if not os.path.exists(storage_path) or storage_path.startswith("s3://"):
                # Use explicit verified temporary file context manager
                with blob_storage.verified_temp_file(
                    storage_path=storage_path,
                    expected_hash=db_doc.content_hash,
                    suffix=f".{ext}",
                ) as verified_path:
                    chunks = ingest_document(
                        verified_path,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        access_level=access_level,
                        allowed_roles=allowed_roles,
                        allowed_user_ids=allowed_user_ids,
                        permission_status="KNOWN",
                    )
            else:
                # Local file: verify checksum before parsing
                _ = blob_storage.download_verified(storage_path, expected_hash=db_doc.content_hash)
                chunks = ingest_document(
                    storage_path,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    access_level=access_level,
                    allowed_roles=allowed_roles,
                    allowed_user_ids=allowed_user_ids,
                    permission_status="KNOWN",
                )
        except StorageIntegrityError as exc:
            logger.error(f"[IngestHandler] Critical integrity violation on doc {document_id}: {exc}")
            doc_repo.update_status(document_id, status="FAILED", error_message="Document integrity check failed (corrupted content).")
            context.db.commit()
            log_security_event(
                event_type="INTEGRITY_VIOLATION",
                tenant_id=tenant_id,
                user_id=owner_id,
                action="document_ingest",
                result="rejected",
                document_id=document_id,
                details={"reason": "SHA-256 checksum mismatch", "task_id": task.id},
            )
            raise NonRetryableTaskError(f"Document integrity check failed: {exc}") from exc
        except (ValueError, FileNotFoundError) as exc:
            doc_repo.update_status(document_id, status="FAILED", error_message=str(exc))
            context.db.commit()
            raise NonRetryableTaskError(f"Document parsing/chunking error: {exc}") from exc
        except json.JSONDecodeError as exc:
            doc_repo.update_status(document_id, status="FAILED", error_message=f"Invalid JSON: {exc.msg}")
            context.db.commit()
            raise NonRetryableTaskError(f"Invalid JSON file format: {exc.msg}") from exc
        except Exception as exc:
            logger.error(f"[IngestHandler] Unexpected ingestion error on {document_id}: {exc}")
            raise RetryableTaskError(f"Transient parsing error: {exc}") from exc

        # ── 4. Second Race Check Before Persisting Chunks ───────────────────
        context.db.expire_all()
        db_doc = doc_repo.get_by_id(document_id, tenant_id=tenant_id)
        if not db_doc or db_doc.status in ["DELETING", "DELETED"]:
            logger.info(f"[IngestHandler] Document '{document_id}' was deleted during chunking. Aborting write.")
            return {"status": "aborted", "reason": "document_deleted_during_chunking"}

        if db_doc.version != current_version:
            logger.warning(
                f"[IngestHandler] Version changed during chunking for doc '{document_id}'. Aborting."
            )
            return {"status": "aborted", "reason": "version_changed_during_chunking"}

        # ── 5. Save Permissions and Chunks in PostgreSQL ────────────────────
        perm_repo.set_permissions(
            document_id=document_id,
            tenant_id=tenant_id,
            roles=allowed_roles,
            user_ids=allowed_user_ids,
        )

        chunk_repo.delete_by_document(document_id, tenant_id=tenant_id)

        db_chunks = []
        for idx, c in enumerate(chunks):
            actual_chunk_id = f"{document_id}_{idx}"
            c.metadata["chunk_id"] = actual_chunk_id
            c.metadata["document_id"] = document_id
            c.metadata["tenant_id"] = tenant_id
            db_chunks.append(
                DBDocumentChunk(
                    chunk_id=actual_chunk_id,
                    document_id=document_id,
                    tenant_id=tenant_id,
                    chunk_index=idx,
                    content_hash=hashlib.sha256(c.page_content.encode("utf-8")).hexdigest(),
                    content=c.page_content,
                    metadata_json=c.metadata,
                )
            )
        chunk_repo.create_batch(db_chunks)
        updated_doc = doc_repo.update_status(document_id, status="INDEXING")
        current_version = updated_doc.version if updated_doc else current_version + 1
        context.db.commit()

        # ── 6. Index into OpenSearch / Search Store ─────────────────────────
        search_store = context.search_store
        if search_store:
            try:
                # Generate embeddings if needed
                rag_service = context.rag_service
                embedder = rag_service.embeddings if (rag_service and hasattr(rag_service, "embeddings")) else None
                
                chunk_payloads = []
                for idx, c in enumerate(chunks):
                    vec = None
                    if embedder:
                        vec = embedder.embed_query(c.page_content)
                    
                    actual_chunk_id = f"{document_id}_{idx}"
                    chunk_payloads.append(
                        ChunkPayload(
                            chunk_id=actual_chunk_id,
                            document_id=document_id,
                            tenant_id=tenant_id,
                            document_version=expected_version,
                            content=c.page_content,
                            content_hash=hashlib.sha256(c.page_content.encode("utf-8")).hexdigest(),
                            embedding=vec,
                            metadata=c.metadata,
                        )
                    )
                search_store.index_chunks(chunk_payloads)
            except Exception as exc:
                logger.error(f"[IngestHandler] OpenSearch indexing failed on doc {document_id}: {exc}")
                raise RetryableTaskError(f"Search store indexing failure: {exc}") from exc

        # Legacy vector DB fallback for in-memory development mode if configured
        if context.rag_service and hasattr(context.rag_service, "vector_db"):
            try:
                rag_service = context.rag_service
                if hasattr(rag_service.vector_db, "vectordb"):
                    try:
                        rag_service.vector_db.vectordb.delete(where={"document_id": document_id})
                    except Exception:
                        pass
                    rag_service.vector_db.vectordb.add_documents(chunks)
                rag_service.retrieval_pipeline.rebuild_bm25()
            except Exception as exc:
                doc_repo.update_status(document_id, status="FAILED", error_message=str(exc))
                context.db.commit()
                logger.error(f"[IngestHandler] Legacy vector DB sync error: {exc}")
                raise RetryableTaskError(f"Legacy vector DB sync failure: {exc}") from exc

        # ── 7. Mark Status INDEXED & Log Audit Event ────────────────────────
        doc_repo.update_status(document_id, status="INDEXED", indexed_at=datetime.now(timezone.utc))
        context.db.commit()

        log_security_event(
            event_type="DOCUMENT_UPLOAD",
            tenant_id=tenant_id,
            user_id=owner_id,
            action="async_ingest",
            result="success",
            document_id=document_id,
            details={
                "task_id": task.id,
                "version": expected_version,
                "num_chunks": len(chunks),
                "access_level": access_level,
            },
        )

        return {
            "status": "INDEXED",
            "document_id": document_id,
            "version": expected_version,
            "num_chunks": len(chunks),
        }
