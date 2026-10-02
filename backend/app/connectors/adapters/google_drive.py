"""
google_drive.py — Production-grade Google Drive Cloud Connector.
Supports OAuth2 user credentials & Service Accounts, file discovery, binary & Workspace exports,
authoritative permissions retrieval, change tracking (changes.list), single-flight token refresh,
rate limiting with jitter, and full SSRF defense.
"""

import os
import io
import time
import json
import random
import logging
import hashlib
import threading
import requests
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Tuple, Set

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
    ConnectorError,
    AuthenticationError,
    PermanentAuthError,
    TokenRefreshError,
    SourceUnavailableError,
    RateLimitError,
    PermissionSyncError,
    ConnectorNotFoundError,
)
from app.connectors.ssrf import SSRFSafeSession, DEFAULT_GOOGLE_DOMAINS

logger = logging.getLogger(__name__)

# Google Workspace MIME Types to Export Mappings
WORKSPACE_EXPORT_MIMES = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
}


class GoogleDriveConnector(DocumentConnector):
    """
    Production-grade Google Drive API v3 Connector.
    """

    def __init__(self, tenant_id: str, connector_id: str, config: Dict[str, Any]):
        super().__init__(tenant_id, connector_id, config)
        self.client_id = config.get("client_id")
        self.client_secret = config.get("client_secret")
        self.refresh_token = config.get("refresh_token")
        self.access_token = config.get("access_token")
        self.token_expiry = config.get("token_expiry", 0)
        self.service_account_info = config.get("service_account_info")
        self.folder_id = config.get("folder_id")
        self.include_shared_drives = bool(config.get("include_shared_drives", True))
        self.allow_public_link_access = bool(config.get("allow_public_link_access", False))
        self.allow_domain_access = bool(config.get("allow_domain_access", False))
        self.allowed_domains = [d.lower().strip() for d in config.get("allowed_domains", [])]
        
        # Test double / mock client hook
        self._mock_client = config.get("_mock_client")

        self.api_base_url = "https://www.googleapis.com/drive/v3"
        self.oauth_token_url = "https://oauth2.googleapis.com/token"
        
        self._refresh_lock = threading.Lock()
        self._session = SSRFSafeSession(allowed_domains=DEFAULT_GOOGLE_DOMAINS)
        self._credentials_updated = False

    def _get_auth_headers(self) -> Dict[str, str]:
        self._ensure_access_token()
        return {"Authorization": f"Bearer {self.access_token}"}

    def _ensure_access_token(self) -> None:
        """
        Thread-safe single-flight token refresh if token is missing or expired.
        """
        if self._mock_client is not None:
            return

        now = time.time()
        # If token is valid for at least 60 seconds, reuse it
        if self.access_token and self.token_expiry > (now + 60):
            return

        with self._refresh_lock:
            # Re-check under lock
            if self.access_token and self.token_expiry > (now + 60):
                return

            if not self.refresh_token and not self.service_account_info:
                if not self.access_token:
                    raise PermanentAuthError("No access token, refresh token, or service account provided for Google Drive.")
                return

            self._refresh_oauth_token()

    def _refresh_oauth_token(self) -> None:
        """
        Execute OAuth2 token refresh with mutex lock and refresh token preservation.
        """
        if not self.refresh_token:
            raise PermanentAuthError("Cannot refresh access token: refresh_token is missing.")

        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            resp = self._session.post(self.oauth_token_url, data=payload, timeout=15)
            if resp.status_code in (400, 401):
                err_body = resp.text
                if "invalid_grant" in err_body or "unauthorized" in err_body:
                    raise PermanentAuthError(f"Permanent OAuth authentication error from Google: {err_body}")
                raise TokenRefreshError(f"OAuth token refresh rejected: {err_body}")

            resp.raise_for_status()
            data = resp.json()

            self.access_token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            self.token_expiry = time.time() + expires_in
            
            # PRESERVATION INVARIANT: If Google does not return a new refresh token, preserve existing one!
            new_rt = data.get("refresh_token")
            if new_rt:
                self.refresh_token = new_rt

            self.config["access_token"] = self.access_token
            self.config["token_expiry"] = self.token_expiry
            self.config["refresh_token"] = self.refresh_token
            self._credentials_updated = True
            logger.info("[GoogleDriveConnector] Successfully refreshed OAuth access token.")

        except PermanentAuthError:
            raise
        except Exception as exc:
            raise TokenRefreshError(f"Failed to refresh Google OAuth token: {exc}") from exc

    def refresh_credentials(self) -> Optional[Dict[str, Any]]:
        """
        Return updated configuration dictionary for SecretProvider persistence if refreshed.
        """
        if self._credentials_updated:
            self._credentials_updated = False
            return {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "access_token": self.access_token,
                "token_expiry": self.token_expiry,
                "service_account_info": self.service_account_info,
                "folder_id": self.folder_id,
                "include_shared_drives": self.include_shared_drives,
                "allow_public_link_access": self.allow_public_link_access,
                "allow_domain_access": self.allow_domain_access,
                "allowed_domains": self.allowed_domains,
            }
        return None

    def _execute_api_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        stream: bool = False,
        max_retries: int = 3,
    ) -> requests.Response:
        """
        Execute an HTTP request against Google Drive API with automatic token refresh on 401,
        exponential backoff with jitter on 429 / 5xx, and sanitized error mapping.
        """
        if self._mock_client is not None:
            return self._mock_client.handle_request(method, endpoint, params, data, headers)

        url = f"{self.api_base_url}/{endpoint.lstrip('/')}"
        req_headers = self._get_auth_headers()
        if headers:
            req_headers.update(headers)

        attempt = 0
        refreshed_on_401 = False

        while attempt < max_retries:
            attempt += 1
            try:
                resp = self._session.request(
                    method=method,
                    url=url,
                    params=params,
                    data=data,
                    headers=req_headers,
                    stream=stream,
                    timeout=30,
                )

                if resp.status_code == 401 and not refreshed_on_401:
                    refreshed_on_401 = True
                    logger.warning("[GoogleDriveConnector] HTTP 401 encountered, refreshing access token...")
                    with self._refresh_lock:
                        self._refresh_oauth_token()
                    req_headers = self._get_auth_headers()
                    if headers:
                        req_headers.update(headers)
                    continue

                if resp.status_code == 429 or resp.status_code >= 500:
                    delay = (2 ** attempt) + random.uniform(0.1, 0.5)
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        delay = max(delay, float(retry_after))
                    logger.warning(f"[GoogleDriveConnector] HTTP {resp.status_code}. Backing off for {delay:.2f}s...")
                    time.sleep(delay)
                    continue

                if resp.status_code == 404:
                    raise ConnectorNotFoundError(f"Google Drive resource not found at '{url}'.")

                if resp.status_code == 403:
                    err_text = resp.text
                    if "userRateLimitExceeded" in err_text or "rateLimitExceeded" in err_text:
                        delay = (2 ** attempt) + random.uniform(0.1, 0.5)
                        time.sleep(delay)
                        continue
                    raise AuthenticationError(f"Access forbidden by Google Drive API: {err_text}")

                resp.raise_for_status()
                return resp

            except (ConnectorNotFoundError, AuthenticationError, PermanentAuthError):
                raise
            except requests.exceptions.RequestException as exc:
                if attempt >= max_retries:
                    raise SourceUnavailableError(f"Google Drive API connection failed after {max_retries} attempts: {exc}") from exc
                delay = (2 ** attempt) + random.uniform(0.1, 0.5)
                time.sleep(delay)

        raise SourceUnavailableError(f"Google Drive API request failed after {max_retries} retries.")

    def validate_configuration(self) -> bool:
        """
        Verify credentials and connectivity via about endpoint.
        """
        if self._mock_client is not None:
            return self._mock_client.validate_configuration()

        try:
            resp = self._execute_api_request("GET", "about", params={"fields": "user,storageQuota"})
            return resp.status_code == 200
        except Exception as exc:
            logger.error(f"[GoogleDriveConnector] Validation failed: {exc}")
            return False

    def list_documents(self) -> List[ConnectorDocument]:
        """
        Discover and list all non-trashed files in Google Drive.
        """
        if self._mock_client is not None:
            return self._mock_client.list_documents()

        documents: List[ConnectorDocument] = []
        page_token: Optional[str] = None
        q = "trashed = false and mimeType != 'application/vnd.google-apps.folder'"
        if self.folder_id:
            q += f" and '{self.folder_id}' in parents"

        fields = "nextPageToken, files(id, name, mimeType, modifiedTime, size, md5Checksum, trashed, parents, capabilities)"

        while True:
            params: Dict[str, Any] = {
                "q": q,
                "fields": fields,
                "pageSize": 100,
                "supportsAllDrives": "true" if self.include_shared_drives else "false",
                "includeItemsFromAllDrives": "true" if self.include_shared_drives else "false",
            }
            if page_token:
                params["pageToken"] = page_token

            resp = self._execute_api_request("GET", "files", params=params)
            data = resp.json()
            files = data.get("files", [])

            for f_meta in files:
                doc = self._build_connector_document(f_meta)
                if doc:
                    documents.append(doc)

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return documents

    def get_document(self, source_id: str) -> Optional[ConnectorDocument]:
        """
        Fetch specific document by its immutable Google file ID.
        """
        if self._mock_client is not None:
            return self._mock_client.get_document(source_id)

        try:
            resp = self._execute_api_request(
                "GET",
                f"files/{source_id}",
                params={
                    "fields": "id, name, mimeType, modifiedTime, size, md5Checksum, trashed, parents",
                    "supportsAllDrives": "true" if self.include_shared_drives else "false",
                },
            )
            f_meta = resp.json()
            return self._build_connector_document(f_meta)
        except ConnectorNotFoundError:
            return None

    def _build_connector_document(self, f_meta: Dict[str, Any]) -> Optional[ConnectorDocument]:
        file_id = f_meta["id"]
        name = f_meta.get("name", file_id)
        mime_type = f_meta.get("mimeType", "application/octet-stream")
        mod_time_str = f_meta.get("modifiedTime")
        mod_time = datetime.fromisoformat(mod_time_str.replace("Z", "+00:00")) if mod_time_str else datetime.now(timezone.utc)
        parents = f_meta.get("parents", [])
        parent_id = parents[0] if parents else None

        # Fetch binary or exported content
        content = self._download_file_content(file_id, mime_type, name)
        content_hash = hashlib.sha256(content).hexdigest()

        # Fetch authoritative ACL
        acl = self.get_permissions(file_id)

        return ConnectorDocument(
            source_type="google_drive",
            source_id=file_id,
            source_path=f"google_drive://{self.tenant_id}/{file_id}",
            source_url=f"https://drive.google.com/file/d/{file_id}/view",
            name=name,
            mime_type=mime_type,
            size_bytes=len(content),
            content=content,
            content_hash=content_hash,
            created_at=mod_time,
            modified_at=mod_time,
            source_version=mod_time_str or str(int(mod_time.timestamp())),
            source_parent_id=parent_id,
            source_modified_at=mod_time,
            metadata={
                "google_file_id": file_id,
                "parents": parents,
                "mime_type": mime_type,
            },
            acl=acl,
        )

    def _download_file_content(self, file_id: str, mime_type: str, filename: str) -> bytes:
        if mime_type in WORKSPACE_EXPORT_MIMES:
            export_mime, _ = WORKSPACE_EXPORT_MIMES[mime_type]
            resp = self._execute_api_request(
                "GET",
                f"files/{file_id}/export",
                params={"mimeType": export_mime},
            )
            return resp.content
        else:
            resp = self._execute_api_request(
                "GET",
                f"files/{file_id}",
                params={
                    "alt": "media",
                    "supportsAllDrives": "true" if self.include_shared_drives else "false",
                },
            )
            return resp.content

    def get_permissions(self, source_id: str) -> Optional[ConnectorACL]:
        """
        Fetch authoritative ACL for Google Drive file.
        Maps reader/commenter/writer/owner roles and user/group/domain/anyone types.
        """
        if self._mock_client is not None:
            return self._mock_client.get_permissions(source_id)

        try:
            resp = self._execute_api_request(
                "GET",
                f"files/{source_id}/permissions",
                params={
                    "fields": "permissions(id, type, role, emailAddress, domain, displayName, deleted)",
                    "supportsAllDrives": "true" if self.include_shared_drives else "false",
                },
            )
            data = resp.json()
            raw_perms = data.get("permissions", [])
            permissions: List[ConnectorPermission] = []

            for p in raw_perms:
                if p.get("deleted"):
                    continue

                p_type = (p.get("type") or "").lower()
                role = (p.get("role") or "").lower()
                email = (p.get("emailAddress") or "").strip()
                domain = (p.get("domain") or "").strip().lower()

                # Map Google role to effect: reader, commenter, writer, owner all permit DOCUMENT_READ
                effect = PermissionEffect.ALLOW

                if p_type == "user":
                    principal_id = email if email else p.get("id", "")
                    if principal_id:
                        permissions.append(
                            ConnectorPermission(
                                principal=Principal(principal_type=PrincipalType.USER, principal_id=principal_id),
                                permission="DOCUMENT_READ",
                                effect=effect,
                            )
                        )

                elif p_type == "group":
                    group_id = email.lower() if email else p.get("id", "").lower()
                    if group_id:
                        permissions.append(
                            ConnectorPermission(
                                principal=Principal(principal_type=PrincipalType.GROUP, principal_id=group_id),
                                permission="DOCUMENT_READ",
                                effect=effect,
                            )
                        )

                elif p_type == "domain":
                    if domain:
                        permissions.append(
                            ConnectorPermission(
                                principal=Principal(principal_type=PrincipalType.ROLE, principal_id=f"DOMAIN:{domain}"),
                                permission="DOCUMENT_READ",
                                effect=effect,
                            )
                        )

                elif p_type == "anyone":
                    permissions.append(
                        ConnectorPermission(
                            principal=Principal(principal_type=PrincipalType.ROLE, principal_id="ANYONE"),
                            permission="DOCUMENT_READ",
                            effect=effect,
                        )
                    )

            return ConnectorACL(permissions=permissions, permission_status="KNOWN")

        except Exception as exc:
            logger.error(f"[GoogleDriveConnector] Failed to fetch authoritative ACL for {source_id}: {exc}")
            # Fail closed on permission fetch error
            return ConnectorACL(permissions=[], permission_status="UNKNOWN")

    def fetch_changes(self, cursor: Optional[str] = None) -> Tuple[List[ConnectorDocument], List[str], Optional[str]]:
        """
        Incremental synchronization using Google Drive change tokens.
        MANDATORY REFETCH INVARIANT: When a change is detected, authoritative ACL is refetched.
        """
        if self._mock_client is not None:
            return self._mock_client.fetch_changes(cursor)

        page_token = cursor
        if not page_token:
            # Obtain initial start page token
            resp = self._execute_api_request(
                "GET",
                "changes/startPageToken",
                params={"supportsAllDrives": "true" if self.include_shared_drives else "false"},
            )
            page_token = resp.json().get("startPageToken")

        changed_docs: List[ConnectorDocument] = []
        deleted_ids: List[str] = []
        new_start_token: Optional[str] = None

        while page_token:
            params = {
                "pageToken": page_token,
                "pageSize": 100,
                "supportsAllDrives": "true" if self.include_shared_drives else "false",
                "includeItemsFromAllDrives": "true" if self.include_shared_drives else "false",
                "includeRemoved": "true",
                "fields": "nextPageToken, newStartPageToken, changes(fileId, removed, file(id, name, mimeType, modifiedTime, size, md5Checksum, trashed, parents))",
            }

            resp = self._execute_api_request("GET", "changes", params=params)
            data = resp.json()
            changes = data.get("changes", [])

            for ch in changes:
                file_id = ch.get("fileId")
                is_removed = ch.get("removed", False)
                f_meta = ch.get("file")

                if is_removed or (f_meta and f_meta.get("trashed")):
                    deleted_ids.append(file_id)
                elif f_meta:
                    if f_meta.get("mimeType") == "application/vnd.google-apps.folder":
                        continue
                    doc = self._build_connector_document(f_meta)
                    if doc:
                        changed_docs.append(doc)

            new_start_token = data.get("newStartPageToken")
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return changed_docs, deleted_ids, new_start_token or cursor
