"""
opensearch_store.py — Production OpenSearch Search Store Adapter.
Supports dynamic k-NN vector search, BM25 lexical keyword search, coarse tenant filtering,
atomic alias switching with rollback index retention, and dynamic embedding dimension validation.
"""

import json
import time
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional, Tuple

from app import config
from app.storage.search.base import SearchStoreInterface
from app.storage.search.models import (
    ChunkPayload,
    SearchHealthResponse,
    SearchStatsResponse,
)
from app.retrieval.models import SearchResult
from app.retrieval.filters import MetadataFilter
from app.observability.metrics import (
    opensearch_requests_total,
    opensearch_duration_seconds,
    opensearch_errors_total,
)
from app.observability.tracing import tracer
from app.observability.errors import classify_error


class OpenSearchTransportInterface:
    """Abstract HTTP transport for OpenSearch REST API calls."""

    def perform_request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        raise NotImplementedError


class HttpOpenSearchTransport(OpenSearchTransportInterface):
    """Production HTTP transport using standard library urllib."""

    def __init__(self, base_url: str, timeout_seconds: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def perform_request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        clean_path = path.lstrip("/")
        url = f"{self.base_url}/{clean_path}"
        if params:
            query_str = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if query_str:
                url = f"{url}?{query_str}"

        req_headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if headers:
            req_headers.update(headers)

        data = None
        if body is not None:
            if isinstance(body, str):
                data = body.encode("utf-8")
            else:
                data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url=url, data=data, headers=req_headers, method=method.upper())

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                status_code = resp.status
                resp_data = resp.read().decode("utf-8")
                parsed = json.loads(resp_data) if resp_data else {}
                return status_code, parsed
        except urllib.error.HTTPError as http_err:
            resp_data = http_err.read().decode("utf-8") if http_err.fp else ""
            try:
                parsed = json.loads(resp_data) if resp_data else {}
            except Exception:
                parsed = {"error": resp_data or str(http_err)}
            return http_err.code, parsed
        except Exception as exc:
            return 503, {"error": f"Connection to OpenSearch failed: {exc}"}


class OpenSearchStore(SearchStoreInterface):
    """
    Production-grade OpenSearch store implementing SearchStoreInterface.
    Provides dynamic vector dimension mapping, BM25 indexing, atomic alias swaps,
    and coarse pre-filtering.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        index_alias: Optional[str] = None,
        transport: Optional[OpenSearchTransportInterface] = None,
        configured_dimension: int = 384,
        timeout_seconds: int = 10,
    ):
        self.base_url = (base_url or getattr(config, "OPENSEARCH_URL", "http://localhost:9200")).rstrip("/")
        self.index_alias = index_alias or getattr(config, "OPENSEARCH_INDEX_PREFIX", "enterprise_rag_chunks")
        self.configured_dimension = configured_dimension
        self.timeout_seconds = timeout_seconds
        self.transport = transport or HttpOpenSearchTransport(self.base_url, timeout_seconds=timeout_seconds)

    @staticmethod
    def generate_mapping(dimension: int) -> Dict[str, Any]:
        """
        Dynamically generate OpenSearch index mapping for the resolved embedding dimension.
        Never hardcodes vector dimension.
        """
        return {
            "settings": {
                "index": {
                    "knn": True,
                    "knn.algo_param.ef_search": 100,
                    "refresh_interval": "1s",
                    "number_of_shards": 1,
                    "number_of_replicas": 1,
                }
            },
            "mappings": {
                "properties": {
                    "chunk_id": {"type": "keyword"},
                    "document_id": {"type": "keyword"},
                    "tenant_id": {"type": "keyword"},
                    "document_version": {"type": "integer"},
                    "content_hash": {"type": "keyword"},
                    "content": {"type": "text", "analyzer": "standard"},
                    "chunk_vector": {
                        "type": "knn_vector",
                        "dimension": dimension,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "nmslib",
                            "parameters": {"ef_construction": 128, "m": 16},
                        },
                    },
                    "metadata": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "keyword"},
                            "filename": {"type": "keyword"},
                            "file_type": {"type": "keyword"},
                            "page": {"type": "integer"},
                            "access_level": {"type": "keyword"},
                            "permission_status": {"type": "keyword"},
                            "allowed_roles": {"type": "keyword"},
                            "allowed_user_ids": {"type": "keyword"},
                            "allowed_groups": {"type": "keyword"},
                            "denied_roles": {"type": "keyword"},
                            "denied_user_ids": {"type": "keyword"},
                            "denied_groups": {"type": "keyword"},
                        },
                    },
                }
            },
        }

    def ensure_index_initialized(self) -> str:
        """Verify alias or physical index exists; creates initial physical index if absent."""
        status_code, resp = self.transport.perform_request("HEAD", f"/{self.index_alias}")
        if status_code == 200:
            return self.index_alias

        # Create initial physical index and alias
        initial_index_name = f"{self.index_alias}_v1"
        mapping = self.generate_mapping(self.configured_dimension)
        c_status, c_resp = self.transport.perform_request("PUT", f"/{initial_index_name}", body=mapping)
        if c_status in {200, 201}:
            # Point alias to v1
            alias_body = {"actions": [{"add": {"index": initial_index_name, "alias": self.index_alias}}]}
            self.transport.perform_request("POST", "/_aliases", body=alias_body)
            return self.index_alias
        return initial_index_name

    def index_chunk(self, payload: ChunkPayload) -> bool:
        """Index a single document chunk."""
        return self.index_chunks([payload]) == 1

    def index_chunks(self, payloads: List[ChunkPayload]) -> int:
        """Bulk index multiple chunks using OpenSearch _bulk API."""
        if not payloads:
            return 0

        t0 = time.perf_counter()
        op_status = "success"
        with tracer.span("opensearch.bulk_index", attributes={"opensearch.count": len(payloads)}):
            try:
                target = self.ensure_index_initialized()
                bulk_lines = []
                for p in payloads:
                    action_line = json.dumps({"index": {"_index": target, "_id": p.chunk_id}})
                    doc_body = {
                        "chunk_id": p.chunk_id,
                        "document_id": p.document_id,
                        "tenant_id": p.tenant_id,
                        "document_version": p.document_version,
                        "content_hash": p.content_hash,
                        "content": p.content,
                        "chunk_vector": p.embedding or [],
                        "metadata": p.metadata or {},
                    }
                    doc_line = json.dumps(doc_body)
                    bulk_lines.append(f"{action_line}\n{doc_line}")

                bulk_payload_str = "\n".join(bulk_lines) + "\n"
                headers = {"Content-Type": "application/x-ndjson"}
                status_code, resp = self.transport.perform_request(
                    "POST",
                    "/_bulk",
                    params={"refresh": "wait_for"},
                    body=bulk_payload_str,
                    headers=headers,
                )

                if status_code not in {200, 201}:
                    op_status = "error"
                    opensearch_errors_total.inc(1, {"operation": "bulk_index", "error_class": f"http_{status_code}"})
                    print(f"[OpenSearchStore] Bulk indexing failed with status {status_code}: {resp}")
                    return 0

                success_count = 0
                items = resp.get("items", [])
                for item in items:
                    op_data = item.get("index", {})
                    if op_data.get("status") in {200, 201}:
                        success_count += 1
                    else:
                        print(f"[OpenSearchStore] Chunk indexing item failed: {op_data.get('error')}")

                return success_count
            except Exception as exc:
                op_status = "error"
                opensearch_errors_total.inc(1, {"operation": "bulk_index", "error_class": classify_error(exc)})
                raise
            finally:
                duration = time.perf_counter() - t0
                opensearch_requests_total.inc(1, {"operation": "bulk_index", "status": op_status})
                opensearch_duration_seconds.observe(duration, {"operation": "bulk_index"})

    def update_chunk_metadata(self, chunk_id: str, metadata: Dict[str, Any], tenant_id: str) -> bool:
        """Update derived metadata fields in OpenSearch without re-embedding."""
        target = self.index_alias
        update_body = {
            "doc": {
                "metadata": metadata,
            }
        }
        status_code, resp = self.transport.perform_request(
            "POST",
            f"/{target}/_update/{chunk_id}",
            params={"refresh": "wait_for"},
            body=update_body,
        )
        return status_code in {200, 201}

    def delete_chunk(self, chunk_id: str, tenant_id: str) -> bool:
        """Delete a specific chunk."""
        target = self.index_alias
        status_code, _ = self.transport.perform_request(
            "DELETE",
            f"/{target}/_doc/{chunk_id}",
            params={"refresh": "wait_for"},
        )
        return status_code in {200, 204}

    def delete_chunks(self, chunk_ids: List[str], tenant_id: str) -> int:
        """Bulk delete chunks by ID."""
        if not chunk_ids:
            return 0
        target = self.index_alias
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"terms": {"chunk_id": chunk_ids}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            }
        }
        status_code, resp = self.transport.perform_request(
            "POST",
            f"/{target}/_delete_by_query",
            params={"refresh": "true"},
            body=query,
        )
        if status_code == 200:
            return resp.get("deleted", 0)
        return 0

    def delete_document(self, document_id: str, tenant_id: str) -> int:
        """Delete all chunks belonging to a document."""
        target = self.index_alias
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"document_id": document_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            }
        }
        status_code, resp = self.transport.perform_request(
            "POST",
            f"/{target}/_delete_by_query",
            params={"refresh": "true"},
            body=query,
        )
        if status_code == 200:
            return resp.get("deleted", 0)
        return 0

    def delete_tenant(self, tenant_id: str) -> int:
        """Delete all chunks belonging to a tenant."""
        target = self.index_alias
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            }
        }
        status_code, resp = self.transport.perform_request(
            "POST",
            f"/{target}/_delete_by_query",
            params={"refresh": "true"},
            body=query,
        )
        if status_code == 200:
            return resp.get("deleted", 0)
        return 0

    def vector_search(
        self,
        query_vector: List[float],
        top_k: int = 10,
        filters: Optional[MetadataFilter] = None,
    ) -> List[SearchResult]:
        """Execute dense vector k-NN search with coarse tenant pre-filtering."""
        t0 = time.perf_counter()
        op_status = "success"
        target = self.index_alias
        with tracer.span("opensearch.vector_search", attributes={"opensearch.top_k": top_k}):
            try:
                coarse_filter_clauses: List[Dict[str, Any]] = []

                # Always enforce tenant isolation at search engine query level
                if filters and filters.auth_context and filters.auth_context.tenant_id:
                    coarse_filter_clauses.append({"term": {"tenant_id": filters.auth_context.tenant_id}})
                elif filters and filters.tenant_id:
                    coarse_filter_clauses.append({"term": {"tenant_id": filters.tenant_id}})

                if filters and filters.document_id:
                    coarse_filter_clauses.append({"term": {"document_id": filters.document_id}})

                query_body: Dict[str, Any] = {
                    "size": top_k,
                    "query": {
                        "knn": {
                            "chunk_vector": {
                                "vector": query_vector,
                                "k": top_k,
                            }
                        }
                    },
                }

                if coarse_filter_clauses:
                    query_body["query"]["knn"]["chunk_vector"]["filter"] = {
                        "bool": {"filter": coarse_filter_clauses}
                    }

                status_code, resp = self.transport.perform_request("POST", f"/{target}/_search", body=query_body)
                if status_code != 200:
                    op_status = "error"
                    opensearch_errors_total.inc(1, {"operation": "vector_search", "error_class": f"http_{status_code}"})
                    return []

                hits = resp.get("hits", {}).get("hits", [])
                results: List[SearchResult] = []
                for h in hits:
                    score = float(h.get("_score", 0.0))
                    source_data = h.get("_source", {})
                    meta = source_data.get("metadata", {})
                    # Embed identifiers back into metadata
                    meta["chunk_id"] = source_data.get("chunk_id", h.get("_id"))
                    meta["document_id"] = source_data.get("document_id")
                    meta["tenant_id"] = source_data.get("tenant_id")
                    meta["document_version"] = source_data.get("document_version", 1)

                    results.append(
                        SearchResult(
                            chunk_id=meta["chunk_id"],
                            document_id=meta["document_id"],
                            content=source_data.get("content", ""),
                            score=score,
                            source=meta.get("source") or meta.get("filename") or "unknown",
                            page=int(meta.get("page", 1)),
                            metadata=meta,
                        )
                    )
                return results
            except Exception as exc:
                op_status = "error"
                opensearch_errors_total.inc(1, {"operation": "vector_search", "error_class": classify_error(exc)})
                raise
            finally:
                duration = time.perf_counter() - t0
                opensearch_requests_total.inc(1, {"operation": "vector_search", "status": op_status})
                opensearch_duration_seconds.observe(duration, {"operation": "vector_search"})

    def keyword_search(
        self,
        query_text: str,
        top_k: int = 10,
        filters: Optional[MetadataFilter] = None,
    ) -> List[SearchResult]:
        """Execute BM25 lexical search with coarse tenant pre-filtering."""
        t0 = time.perf_counter()
        op_status = "success"
        target = self.index_alias
        with tracer.span("opensearch.keyword_search", attributes={"opensearch.top_k": top_k}):
            try:
                coarse_filter_clauses: List[Dict[str, Any]] = []

                if filters and filters.auth_context and filters.auth_context.tenant_id:
                    coarse_filter_clauses.append({"term": {"tenant_id": filters.auth_context.tenant_id}})
                elif filters and filters.tenant_id:
                    coarse_filter_clauses.append({"term": {"tenant_id": filters.tenant_id}})

                if filters and filters.document_id:
                    coarse_filter_clauses.append({"term": {"document_id": filters.document_id}})

                query_body: Dict[str, Any] = {
                    "size": top_k,
                    "query": {
                        "bool": {
                            "must": [
                                {
                                    "match": {
                                        "content": {
                                            "query": query_text,
                                            "operator": "or",
                                        }
                                    }
                                }
                            ],
                            "filter": coarse_filter_clauses,
                        }
                    },
                }

                status_code, resp = self.transport.perform_request("POST", f"/{target}/_search", body=query_body)
                if status_code != 200:
                    op_status = "error"
                    opensearch_errors_total.inc(1, {"operation": "keyword_search", "error_class": f"http_{status_code}"})
                    return []

                hits = resp.get("hits", {}).get("hits", [])
                results: List[SearchResult] = []
                for h in hits:
                    score = float(h.get("_score", 0.0))
                    source_data = h.get("_source", {})
                    meta = source_data.get("metadata", {})
                    meta["chunk_id"] = source_data.get("chunk_id", h.get("_id"))
                    meta["document_id"] = source_data.get("document_id")
                    meta["tenant_id"] = source_data.get("tenant_id")
                    meta["document_version"] = source_data.get("document_version", 1)

                    results.append(
                        SearchResult(
                            chunk_id=meta["chunk_id"],
                            document_id=meta["document_id"],
                            content=source_data.get("content", ""),
                            score=score,
                            source=meta.get("source") or meta.get("filename") or "unknown",
                            page=int(meta.get("page", 1)),
                            metadata=meta,
                        )
                    )
                return results
            except Exception as exc:
                op_status = "error"
                opensearch_errors_total.inc(1, {"operation": "keyword_search", "error_class": classify_error(exc)})
                raise
            finally:
                duration = time.perf_counter() - t0
                opensearch_requests_total.inc(1, {"operation": "keyword_search", "status": op_status})
                opensearch_duration_seconds.observe(duration, {"operation": "keyword_search"})

    def count(self, tenant_id: Optional[str] = None) -> int:
        """Return total chunk count."""
        target = self.index_alias
        body: Dict[str, Any] = {}
        if tenant_id:
            body = {"query": {"term": {"tenant_id": tenant_id}}}
        status_code, resp = self.transport.perform_request("POST", f"/{target}/_count", body=body)
        if status_code == 200:
            return resp.get("count", 0)
        return 0

    def get_active_dimension(self) -> int:
        """Query index mapping and return dynamic vector dimension."""
        target = self.index_alias
        status_code, resp = self.transport.perform_request("GET", f"/{target}/_mapping")
        if status_code != 200 or not resp:
            return self.configured_dimension

        # Extract dimension from first mapping response
        for _, index_data in resp.items():
            props = index_data.get("mappings", {}).get("properties", {})
            dim = props.get("chunk_vector", {}).get("dimension")
            if dim and isinstance(dim, int):
                return dim
        return self.configured_dimension

    def health_check(self) -> SearchHealthResponse:
        """Verify OpenSearch cluster health, connectivity, and dimension alignment."""
        t0 = time.perf_counter()
        status_code, resp = self.transport.perform_request("GET", "/_cluster/health")
        latency = (time.perf_counter() - t0) * 1000

        if status_code != 200:
            return SearchHealthResponse(
                status="UNHEALTHY",
                backend="opensearch",
                cluster_healthy=False,
                vector_ready=False,
                keyword_ready=False,
                configured_dimension=self.configured_dimension,
                index_dimension=0,
                dimension_aligned=False,
                latency_ms=latency,
                details={"error": f"OpenSearch cluster health query returned HTTP {status_code}"},
            )

        cluster_status = resp.get("status", "").lower()
        cluster_healthy = cluster_status in {"green", "yellow"}
        index_dimension = self.get_active_dimension()
        dimension_aligned = index_dimension == self.configured_dimension

        overall_status = "HEALTHY" if (cluster_healthy and dimension_aligned) else "DEGRADED"
        if not cluster_healthy:
            overall_status = "UNHEALTHY"
        elif not dimension_aligned:
            overall_status = "DEGRADED"

        return SearchHealthResponse(
            status=overall_status,
            backend="opensearch",
            cluster_healthy=cluster_healthy,
            vector_ready=cluster_healthy and dimension_aligned,
            keyword_ready=cluster_healthy,
            configured_dimension=self.configured_dimension,
            index_dimension=index_dimension,
            dimension_aligned=dimension_aligned,
            latency_ms=latency,
            details=resp,
        )

    def get_statistics(self, tenant_id: Optional[str] = None) -> SearchStatsResponse:
        """Query statistics and aggregation breakdown."""
        target = self.index_alias
        agg_query: Dict[str, Any] = {
            "size": 0,
            "aggs": {
                "unique_docs": {"cardinality": {"field": "document_id"}},
                "tenant_breakdown": {"terms": {"field": "tenant_id", "size": 100}},
            },
        }
        if tenant_id:
            agg_query["query"] = {"term": {"tenant_id": tenant_id}}

        status_code, resp = self.transport.perform_request("POST", f"/{target}/_search", body=agg_query)
        total_chunks = self.count(tenant_id)
        if status_code != 200:
            return SearchStatsResponse(
                total_chunks=total_chunks,
                total_documents=0,
                tenant_count=1 if tenant_id else 0,
                index_name=self.index_alias,
                backend="opensearch",
            )

        aggs = resp.get("aggregations", {})
        total_docs = aggs.get("unique_docs", {}).get("value", 0)
        buckets = aggs.get("tenant_breakdown", {}).get("buckets", [])
        tenant_dist = {b["key"]: b["doc_count"] for b in buckets}

        return SearchStatsResponse(
            total_chunks=total_chunks,
            total_documents=total_docs,
            tenant_count=len(tenant_dist),
            index_name=self.index_alias,
            backend="opensearch",
            tenant_distribution=tenant_dist,
            details=resp,
        )

    def create_shadow_index(self, shadow_index_name: str, dimension: int) -> bool:
        """Create a new shadow index with dynamically configured vector dimension."""
        mapping = self.generate_mapping(dimension)
        status_code, _ = self.transport.perform_request("PUT", f"/{shadow_index_name}", body=mapping)
        return status_code in {200, 201}

    def swap_alias(
        self,
        active_alias: str,
        new_index: str,
        previous_alias_tag: Optional[str] = None,
    ) -> bool:
        """
        Atomically swap active_alias to new_index.
        Retains previous index with previous_alias_tag for rollback safety.
        """
        # Find which index currently holds active_alias
        status_code, alias_data = self.transport.perform_request("GET", f"/_alias/{active_alias}")
        old_indexes = list(alias_data.keys()) if status_code == 200 else []

        actions: List[Dict[str, Any]] = [
            {"add": {"index": new_index, "alias": active_alias}}
        ]
        for old_idx in old_indexes:
            actions.append({"remove": {"index": old_idx, "alias": active_alias}})
            if previous_alias_tag:
                # Retain previous index under rollback alias tag
                actions.append({"add": {"index": old_idx, "alias": previous_alias_tag}})

        body = {"actions": actions}
        status_code, resp = self.transport.perform_request("POST", "/_aliases", body=body)
        return status_code == 200

    def delete_index(self, index_name: str) -> bool:
        """Delete an underlying physical index."""
        status_code, _ = self.transport.perform_request("DELETE", f"/{index_name}")
        return status_code in {200, 204}
