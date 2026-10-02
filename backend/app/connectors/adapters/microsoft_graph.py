"""
microsoft_graph.py — Production-grade Microsoft Graph Cloud Connector (SharePoint & OneDrive).
Supports OAuth2 Client Credentials & Delegated tokens, site/drive discovery, binary downloads,
authoritative permissions retrieval with inheritance, delta queries (/drive/root/delta),
single-flight token refresh, Retry-After throttling handling, and full SSRF defense.
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
from app.connectors.ssrf import SSRFSafeSession, DEFAULT_MICROSOFT_DOMAINS

logger = logging.getLogger(__name__)


class MicrosoftGraphConnector(DocumentConnector):
    """
    Production-grade Microsoft Graph API v1.0 Connector for SharePoint and OneDrive.
    """

    def __init__(self, tenant_id: str, connector_id: str, config: Dict[str, Any]):
        super().__init__(tenant_id, connector_id, config)
        self.client_id = config.get("client_id")
        self.client_secret = config.get("client_secret")
        self.aad_tenant_id = config.get("tenant_id") or config.get("aad_tenant_id", "common")
        self.refresh_token = config.get("refresh_token")
        self.access_token = config.get("access_token")
        self.token_expiry = config.get("token_expiry", 0)
        self.grant_type = config.get("grant_type", "client_credentials" if not self.refresh_token else "refresh_token")
        self.drive_id = config.get("drive_id")
        self.site_id = config.get("site_id")
        self.allow_public_link_access = bool(config.get("allow_public_link_access", False))
        self.allow_domain_access = bool(config.get("allow_domain_access", False))
        self.allowed_domains = [d.lower().strip() for d in config.get("allowed_domains", [])]
        
        # Test double / mock hook
        self._mock_client = config.get("_mock_client")

        self.api_base_url = "https://graph.microsoft.com/v1.0"
        self.oauth_token_url = f"https://login.microsoftonline.com/{self.aad_tenant_id}/oauth2/v2.0/token"

        self._refresh_lock = threading.Lock()
        self._session = SSRFSafeSession(allowed_domains=DEFAULT_MICROSOFT_DOMAINS)
        self._credentials_updated = False

    def _get_auth_headers(self) -> Dict[str, str]:
        self._ensure_access_token()
        return {"Authorization": f"Bearer {self.access_token}"}

    def _ensure_access_token(self) -> None:
        if self._mock_client is not None:
            return

        now = time.time()
        if self.access_token and self.token_expiry > (now + 60):
            return

        with self._refresh_lock:
            if self.access_token and self.token_expiry > (now + 60):
                return

            self._refresh_token_call()

    def _refresh_token_call(self) -> None:
        """
        Thread-safe single-flight token acquisition/refresh for Microsoft Graph.
        """
        if self.grant_type == "client_credentials":
            payload = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            }
        elif self.grant_type == "refresh_token" or self.refresh_token:
            payload = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
                "scope": "https://graph.microsoft.com/.default offline_access",
            }
        else:
            if not self.access_token:
                raise PermanentAuthError("Missing Microsoft Graph credentials (no client_secret or refresh_token).")
            return

        try:
            resp = self._session.post(self.oauth_token_url, data=payload, timeout=15)
            if resp.status_code in (400, 401):
                err_text = resp.text
                if "invalid_client" in err_text or "invalid_grant" in err_text or "unauthorized" in err_text:
                    raise PermanentAuthError(f"Permanent OAuth authentication failure from Microsoft: {err_text}")
                raise TokenRefreshError(f"Microsoft token request rejected: {err_text}")

            resp.raise_for_status()
            data = resp.json()

            self.access_token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            self.token_expiry = time.time() + expires_in

            # PRESERVATION INVARIANT: Preserve existing refresh token if not returned
            new_rt = data.get("refresh_token")
            if new_rt:
                self.refresh_token = new_rt

            self.config["access_token"] = self.access_token
            self.config["token_expiry"] = self.token_expiry
            self.config["refresh_token"] = self.refresh_token
            self._credentials_updated = True
            logger.info("[MicrosoftGraphConnector] Successfully acquired/refreshed Microsoft Graph access token.")

        except PermanentAuthError:
            raise
        except Exception as exc:
            raise TokenRefreshError(f"Failed to obtain Microsoft Graph token: {exc}") from exc

    def refresh_credentials(self) -> Optional[Dict[str, Any]]:
        if self._credentials_updated:
            self._credentials_updated = False
            return {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "tenant_id": self.aad_tenant_id,
                "refresh_token": self.refresh_token,
                "access_token": self.access_token,
                "token_expiry": self.token_expiry,
                "grant_type": self.grant_type,
                "drive_id": self.drive_id,
                "site_id": self.site_id,
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
        Execute an HTTP request against Microsoft Graph API with Retry-After throttling support.
        """
        if self._mock_client is not None:
            return self._mock_client.handle_request(method, endpoint, params, data, headers)

        if endpoint.startswith("https://"):
            url = endpoint
        else:
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
                    logger.warning("[MicrosoftGraphConnector] HTTP 401 encountered, refreshing access token...")
                    with self._refresh_lock:
                        self._refresh_token_call()
                    req_headers = self._get_auth_headers()
                    if headers:
                        req_headers.update(headers)
                    continue

                if resp.status_code == 429 or resp.status_code in (503, 504):
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if (retry_after and retry_after.isdigit()) else ((2 ** attempt) + random.uniform(0.1, 0.5))
                    logger.warning(f"[MicrosoftGraphConnector] Throttling (HTTP {resp.status_code}). Backing off for {delay:.2f}s...")
                    time.sleep(delay)
                    continue

                if resp.status_code == 404:
                    raise ConnectorNotFoundError(f"Microsoft Graph resource not found at '{url}'.")

                if resp.status_code == 403:
                    raise AuthenticationError(f"Access forbidden by Microsoft Graph API: {resp.text}")

                resp.raise_for_status()
                return resp

            except (ConnectorNotFoundError, AuthenticationError, PermanentAuthError):
                raise
            except requests.exceptions.RequestException as exc:
                if attempt >= max_retries:
                    raise SourceUnavailableError(f"Microsoft Graph API connection failed: {exc}") from exc
                delay = (2 ** attempt) + random.uniform(0.1, 0.5)
                time.sleep(delay)

        raise SourceUnavailableError(f"Microsoft Graph API request failed after {max_retries} retries.")

    def _get_drive_endpoint(self) -> str:
        if self.drive_id:
            return f"drives/{self.drive_id}"
        elif self.site_id:
            return f"sites/{self.site_id}/drive"
        return "me/drive"

    def validate_configuration(self) -> bool:
        if self._mock_client is not None:
            return self._mock_client.validate_configuration()

        try:
            drive_ep = self._get_drive_endpoint()
            resp = self._execute_api_request("GET", drive_ep)
            return resp.status_code == 200
        except Exception as exc:
            logger.error(f"[MicrosoftGraphConnector] Validation failed: {exc}")
            return False

    def list_documents(self) -> List[ConnectorDocument]:
        """
        Discover and list all files in the targeted OneDrive or SharePoint drive.
        """
        if self._mock_client is not None:
            return self._mock_client.list_documents()

        documents: List[ConnectorDocument] = []
        drive_ep = self._get_drive_endpoint()
        url = f"{drive_ep}/root/children"

        while url:
            resp = self._execute_api_request("GET", url)
            data = resp.json()
            items = data.get("value", [])

            for item in items:
                # Skip folders
                if "folder" in item:
                    continue

                doc = self._build_connector_document(item)
                if doc:
                    documents.append(doc)

            url = data.get("@odata.nextLink")

        return documents

    def get_document(self, source_id: str) -> Optional[ConnectorDocument]:
        if self._mock_client is not None:
            return self._mock_client.get_document(source_id)

        try:
            drive_ep = self._get_drive_endpoint()
            resp = self._execute_api_request("GET", f"{drive_ep}/items/{source_id}")
            item = resp.json()
            return self._build_connector_document(item)
        except ConnectorNotFoundError:
            return None

    def _build_connector_document(self, item: Dict[str, Any]) -> Optional[ConnectorDocument]:
        item_id = item["id"]
        name = item.get("name", item_id)
        web_url = item.get("webUrl", f"https://graph.microsoft.com/v1.0/items/{item_id}")
        file_facet = item.get("file", {})
        mime_type = file_facet.get("mimeType", "application/octet-stream")
        mod_time_str = item.get("lastModifiedDateTime")
        mod_time = datetime.fromisoformat(mod_time_str.replace("Z", "+00:00")) if mod_time_str else datetime.now(timezone.utc)
        parent_ref = item.get("parentReference", {})
        parent_id = parent_ref.get("id")

        # Fetch binary content
        drive_ep = self._get_drive_endpoint()
        content_resp = self._execute_api_request("GET", f"{drive_ep}/items/{item_id}/content")
        content = content_resp.content
        content_hash = hashlib.sha256(content).hexdigest()

        # Fetch authoritative ACL
        acl = self.get_permissions(item_id)

        return ConnectorDocument(
            source_type="microsoft_graph",
            source_id=item_id,
            source_path=f"microsoft_graph://{self.tenant_id}/{item_id}",
            source_url=web_url,
            name=name,
            mime_type=mime_type,
            size_bytes=len(content),
            content=content,
            content_hash=content_hash,
            created_at=mod_time,
            modified_at=mod_time,
            source_version=item.get("eTag") or mod_time_str or str(int(mod_time.timestamp())),
            source_parent_id=parent_id,
            source_modified_at=mod_time,
            metadata={
                "graph_item_id": item_id,
                "drive_id": self.drive_id,
                "site_id": self.site_id,
                "cTag": item.get("cTag"),
                "eTag": item.get("eTag"),
            },
            acl=acl,
        )

    def get_permissions(self, source_id: str) -> Optional[ConnectorACL]:
        """
        Fetch authoritative ACL for SharePoint / OneDrive item.
        Extracts grantedToV2 (user, group, siteGroup) and grantedTo identities with roles.
        """
        if self._mock_client is not None:
            return self._mock_client.get_permissions(source_id)

        try:
            drive_ep = self._get_drive_endpoint()
            resp = self._execute_api_request("GET", f"{drive_ep}/items/{source_id}/permissions")
            data = resp.json()
            raw_perms = data.get("value", [])
            permissions: List[ConnectorPermission] = []

            for p in raw_perms:
                roles = [r.lower() for r in p.get("roles", [])]
                if not any(r in ("read", "write", "owner") for r in roles):
                    continue

                effect = PermissionEffect.ALLOW

                # 1. grantedToV2 facet
                granted_to_v2 = p.get("grantedToV2", {})
                if granted_to_v2:
                    if "user" in granted_to_v2:
                        u_meta = granted_to_v2["user"]
                        uid = u_meta.get("email") or u_meta.get("userPrincipalName") or u_meta.get("id")
                        if uid:
                            permissions.append(
                                ConnectorPermission(
                                    principal=Principal(principal_type=PrincipalType.USER, principal_id=uid),
                                    permission="DOCUMENT_READ",
                                    effect=effect,
                                )
                            )
                    elif "group" in granted_to_v2 or "siteGroup" in granted_to_v2:
                        g_meta = granted_to_v2.get("group") or granted_to_v2.get("siteGroup")
                        gid = g_meta.get("email") or g_meta.get("displayName") or g_meta.get("id")
                        if gid:
                            permissions.append(
                                ConnectorPermission(
                                    principal=Principal(principal_type=PrincipalType.GROUP, principal_id=gid.lower()),
                                    permission="DOCUMENT_READ",
                                    effect=effect,
                                )
                            )

                # 2. grantedTo facet (fallback)
                granted_to = p.get("grantedTo", {})
                if granted_to and not granted_to_v2:
                    if "user" in granted_to:
                        u_meta = granted_to["user"]
                        uid = u_meta.get("email") or u_meta.get("userPrincipalName") or u_meta.get("id")
                        if uid:
                            permissions.append(
                                ConnectorPermission(
                                    principal=Principal(principal_type=PrincipalType.USER, principal_id=uid),
                                    permission="DOCUMENT_READ",
                                    effect=effect,
                                )
                            )
                    elif "group" in granted_to:
                        g_meta = granted_to["group"]
                        gid = g_meta.get("email") or g_meta.get("displayName") or g_meta.get("id")
                        if gid:
                            permissions.append(
                                ConnectorPermission(
                                    principal=Principal(principal_type=PrincipalType.GROUP, principal_id=gid.lower()),
                                    permission="DOCUMENT_READ",
                                    effect=effect,
                                )
                            )

                # 3. Sharing links
                link_facet = p.get("link", {})
                if link_facet:
                    scope = (link_facet.get("scope") or "").lower()
                    if scope == "anonymous":
                        permissions.append(
                            ConnectorPermission(
                                principal=Principal(principal_type=PrincipalType.ROLE, principal_id="PUBLIC"),
                                permission="DOCUMENT_READ",
                                effect=effect,
                            )
                        )
                    elif scope == "organization":
                        permissions.append(
                            ConnectorPermission(
                                principal=Principal(principal_type=PrincipalType.ROLE, principal_id="DOMAIN:ORGANIZATION"),
                                permission="DOCUMENT_READ",
                                effect=effect,
                            )
                        )

            return ConnectorACL(permissions=permissions, permission_status="KNOWN")

        except Exception as exc:
            logger.error(f"[MicrosoftGraphConnector] Failed to fetch ACL for {source_id}: {exc}")
            return ConnectorACL(permissions=[], permission_status="UNKNOWN")

    def fetch_changes(self, cursor: Optional[str] = None) -> Tuple[List[ConnectorDocument], List[str], Optional[str]]:
        """
        Incremental synchronization using Microsoft Graph delta queries (/drive/root/delta).
        MANDATORY REFETCH INVARIANT: When a change is detected, authoritative ACL is refetched.
        """
        if self._mock_client is not None:
            return self._mock_client.fetch_changes(cursor)

        drive_ep = self._get_drive_endpoint()
        if cursor and cursor.startswith("https://"):
            url = cursor
        else:
            url = f"{drive_ep}/root/delta"
            if cursor:
                url += f"?token={cursor}"

        changed_docs: List[ConnectorDocument] = []
        deleted_ids: List[str] = []
        delta_link: Optional[str] = None

        while url:
            resp = self._execute_api_request("GET", url)
            data = resp.json()
            items = data.get("value", [])

            for item in items:
                item_id = item["id"]
                # Deletion detection via @removed facet
                if "@removed" in item or item.get("deleted"):
                    deleted_ids.append(item_id)
                elif "folder" not in item:
                    doc = self._build_connector_document(item)
                    if doc:
                        changed_docs.append(doc)

            delta_link = data.get("@odata.deltaLink")
            url = data.get("@odata.nextLink")
            if not url:
                break

        return changed_docs, deleted_ids, delta_link or cursor
