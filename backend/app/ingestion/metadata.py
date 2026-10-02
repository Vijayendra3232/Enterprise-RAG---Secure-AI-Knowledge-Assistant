"""
metadata.py — Document and chunk metadata utilities.

Every document that enters the ingestion pipeline is assigned a stable
document_id (SHA-256 hash of file content), rich base metadata, and
extensible fields for future multi-tenancy, access control, etc.
"""

import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Document ID
# ---------------------------------------------------------------------------

def generate_document_id(file_path: str) -> str:
    """
    Generate a stable, content-based document ID using SHA-256.

    Two files with identical content will share the same document_id,
    regardless of their filenames or locations. This allows:
      - Deduplication
      - Two files with the same name to coexist (different content → different ID)
      - Stable references even when files are renamed

    Args:
        file_path: Absolute path to the file on disk.

    Returns:
        64-character hex string (SHA-256 digest).
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            sha256.update(block)
    return sha256.hexdigest()


# ---------------------------------------------------------------------------
# Base Metadata
# ---------------------------------------------------------------------------

def build_base_metadata(
    file_path: str,
    document_id: str,
    *,
    document_version: str = "1.0",
    tenant_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    access_level: Optional[str] = None,
    allowed_roles: Optional[list] = None,
    allowed_user_ids: Optional[list] = None,
    permission_status: str = "KNOWN",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build the standard metadata dictionary for a document.

    Mandatory fields (always present):
        document_id        — stable SHA-256 content hash
        filename           — basename of the original file
        file_type          — lowercase extension without the dot
        source             — absolute path of the file on disk
        page               — default 1; loaders override per page
        chunk_id           — None until chunking; set by the pipeline
        ingestion_timestamp — ISO-8601 UTC timestamp of when ingestion ran
        document_version   — default "1.0"; can be set by caller

    Access control fields:
        tenant_id          — multi-tenant namespace
        owner_id           — document owner / uploader user ID
        access_level       — "PRIVATE", "USER", "ROLE", "TENANT", "PUBLIC"
        allowed_roles      — list of roles allowed to access
        allowed_user_ids   — list of specific user IDs allowed to access
        permission_status  — "KNOWN", "UNKNOWN" (fails safe)
    """
    filename = os.path.basename(file_path)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "unknown"

    metadata: Dict[str, Any] = {
        # --- Core fields ---
        "document_id": document_id,
        "filename": filename,
        "file_type": ext,
        "source": file_path,
        "page": 1,
        "chunk_id": None,
        "ingestion_timestamp": datetime.now(timezone.utc).isoformat(),
        "document_version": document_version,
        # --- Security & Access Control ---
        "tenant_id": tenant_id,
        "owner_id": owner_id,
        "access_level": access_level,
        "allowed_roles": allowed_roles or [],
        "allowed_user_ids": allowed_user_ids or [],
        "permission_status": permission_status,
    }

    if extra:
        metadata.update(extra)

    return metadata

