"""
local.py — Deterministic Local Filesystem Connector Adapter.
Discovers local documents, calculates SHA-256 hashes, and parses explicit sidecar ACL JSON files.
Strictly ensures that folder names are NEVER used to infer permissions.
"""

import hashlib
import json
import os
import mimetypes
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from app.connectors.base import (
    DocumentConnector,
    ConnectorDocument,
    ConnectorACL,
    ConnectorPermission,
    Principal,
    PrincipalType,
    PermissionEffect,
)
from app.connectors.errors import (
    SourceUnavailableError,
    MalformedDocumentError,
    PermissionSyncError,
)
from app.ingestion.loaders import SUPPORTED_EXTENSIONS


class LocalConnector(DocumentConnector):
    """
    Local filesystem connector for discovering and loading files from a directory.
    Uses `{filename}.acl.json` sidecar files for authoritative ACL definitions.
    """

    def __init__(self, tenant_id: str, connector_id: str, config: Dict[str, Any]):
        super().__init__(tenant_id, connector_id, config)
        self.directory_path = config.get("directory_path") or config.get("path") or ""

    def validate_configuration(self) -> bool:
        """Verify that directory path is provided, exists, and is readable."""
        if not self.directory_path:
            raise SourceUnavailableError("Missing required 'directory_path' in connector configuration.")
        if not os.path.exists(self.directory_path):
            raise SourceUnavailableError(f"Directory path does not exist: {self.directory_path}")
        if not os.path.isdir(self.directory_path):
            raise SourceUnavailableError(f"Specified path is not a directory: {self.directory_path}")
        return True

    def list_documents(self) -> List[ConnectorDocument]:
        """
        Recursively scan directory for documents, ignoring .acl.json sidecars as document items.
        """
        self.validate_configuration()
        documents: List[ConnectorDocument] = []

        try:
            for root, _, files in os.walk(self.directory_path):
                for file in files:
                    # Skip sidecar ACL files and hidden/dot files
                    if file.endswith(".acl.json") or file.startswith("."):
                        continue

                    ext = file.rsplit(".", 1)[-1].lower() if "." in file else ""
                    if ext not in SUPPORTED_EXTENSIONS:
                        continue

                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, self.directory_path).replace("\\", "/")
                    doc = self._load_local_document(rel_path, full_path)
                    if doc:
                        documents.append(doc)
        except Exception as e:
            if isinstance(e, (SourceUnavailableError, MalformedDocumentError)):
                raise
            raise SourceUnavailableError(f"Failed to scan directory '{self.directory_path}': {e}") from e

        return documents

    def get_document(self, source_id: str) -> Optional[ConnectorDocument]:
        """
        Retrieve a specific document by relative path (source_id).
        """
        self.validate_configuration()
        # Prevent directory traversal
        norm_source_id = os.path.normpath(source_id).replace("\\", "/")
        if norm_source_id.startswith(".."):
            raise MalformedDocumentError(f"Invalid source_id (path traversal attempt): {source_id}")

        full_path = os.path.join(self.directory_path, norm_source_id)
        if not os.path.exists(full_path) or not os.path.isfile(full_path):
            return None

        return self._load_local_document(norm_source_id, full_path)

    def get_permissions(self, source_id: str) -> Optional[ConnectorACL]:
        """
        Fetch authoritative ACL from sidecar `{filename}.acl.json`.
        If missing or malformed, marks permission_status as UNKNOWN (fails closed).
        """
        self.validate_configuration()
        norm_source_id = os.path.normpath(source_id).replace("\\", "/")
        full_path = os.path.join(self.directory_path, norm_source_id)
        return self._parse_sidecar_acl(full_path)

    def get_changes(self, since_timestamp: Optional[datetime] = None) -> List[ConnectorDocument]:
        """
        Fetch documents modified since since_timestamp.
        """
        all_docs = self.list_documents()
        if since_timestamp is None:
            return all_docs

        changed: List[ConnectorDocument] = []
        for doc in all_docs:
            if doc.modified_at:
                # Ensure tz-aware comparison
                doc_mtime = doc.modified_at
                cmp_time = since_timestamp
                if doc_mtime.tzinfo is None:
                    doc_mtime = doc_mtime.replace(tzinfo=timezone.utc)
                if cmp_time.tzinfo is None:
                    cmp_time = cmp_time.replace(tzinfo=timezone.utc)

                if doc_mtime >= cmp_time:
                    changed.append(doc)
            else:
                changed.append(doc)
        return changed

    def _load_local_document(self, rel_path: str, full_path: str) -> Optional[ConnectorDocument]:
        try:
            with open(full_path, "rb") as f:
                content = f.read()

            content_hash = hashlib.sha256(content).hexdigest()
            stat = os.stat(full_path)
            size_bytes = stat.st_size
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            ctime = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc)

            filename = os.path.basename(full_path)
            mime_type, _ = mimetypes.guess_type(full_path)
            if not mime_type:
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                mime_type = f"application/{ext}" if ext else "application/octet-stream"

            acl = self._parse_sidecar_acl(full_path)

            return ConnectorDocument(
                source_type="local",
                source_id=rel_path,
                source_path=full_path,
                source_url=f"file://{os.path.abspath(full_path)}",
                name=filename,
                mime_type=mime_type,
                size_bytes=size_bytes,
                content=content,
                content_hash=content_hash,
                created_at=ctime,
                modified_at=mtime,
                metadata={
                    "connector_id": self.connector_id,
                    "tenant_id": self.tenant_id,
                    "relative_path": rel_path,
                },
                acl=acl,
            )
        except Exception as e:
            raise MalformedDocumentError(f"Failed to read file '{full_path}': {e}") from e

    def _parse_sidecar_acl(self, file_path: str) -> ConnectorACL:
        """
        Read explicit ACL sidecar `{file_path}.acl.json`.
        If missing, invalid JSON, or malformed schema -> returns permission_status='UNKNOWN'.
        """
        acl_path = f"{file_path}.acl.json"
        if not os.path.exists(acl_path):
            # Missing ACL fails closed
            return ConnectorACL(permissions=[], permission_status="UNKNOWN")

        try:
            with open(acl_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            raw_perms = data.get("permissions", [])
            parsed_perms: List[ConnectorPermission] = []

            for p in raw_perms:
                p_type_str = p.get("principal_type", "").upper()
                p_id = str(p.get("principal_id", "")).strip()
                effect_str = p.get("effect", "ALLOW").upper()

                if not p_type_str or not p_id:
                    continue

                if p_type_str not in PrincipalType.__members__:
                    # Unsupported principal type fails safe
                    continue

                p_type = PrincipalType[p_type_str]
                effect = PermissionEffect.DENY if effect_str == "DENY" else PermissionEffect.ALLOW

                parsed_perms.append(
                    ConnectorPermission(
                        principal=Principal(
                            principal_type=p_type,
                            principal_id=p_id,
                            display_name=p.get("display_name"),
                        ),
                        permission=p.get("permission", "DOCUMENT_READ"),
                        effect=effect,
                    )
                )

            return ConnectorACL(
                permissions=parsed_perms,
                permission_status=data.get("permission_status", "KNOWN").upper(),
            )
        except Exception as e:
            # Malformed JSON or unreadable ACL fails closed
            return ConnectorACL(permissions=[], permission_status="UNKNOWN")
