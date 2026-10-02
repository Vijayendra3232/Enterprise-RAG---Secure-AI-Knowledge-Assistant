import time
from typing import List, Optional
from app import config
from app.retrieval.models import SearchResult, RetrievalDiagnostics, RetrievalResponse
from app.retrieval.filters import MetadataFilter
from app.retrieval.vector import VectorRetriever
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.fusion import QueryExpander, reciprocal_rank_fusion
from app.retrieval.reranker import CrossEncoderReranker
from app.authorization.context import AuthorizationContext
from app.authorization.filters import is_document_accessible

class RetrievalPipeline:
    """
    Advanced retrieval engine orchestrating metadata filters, query expansion,
    vector/BM25 retrievers, reciprocal rank fusion (RRF), and cross-encoder reranking.
    Supports selecting strategies: 'vector', 'bm25', 'hybrid', 'fusion'.
    """
    def __init__(
        self,
        vector_store=None,
        search_store=None,
        strategy: str = config.RETRIEVAL_STRATEGY,
        enable_reranker: bool = config.ENABLE_RERANKER,
        reranker_model: str = config.RERANKER_MODEL
    ):
        self.vector_store = vector_store
        self.search_store = search_store or (vector_store if hasattr(vector_store, "vector_search") else None)
        self.strategy = strategy.lower()

        # Initialize core components with search_store or vector_store
        target_store = self.search_store or self.vector_store
        self.vector_retriever = VectorRetriever(target_store)
        self.bm25_retriever = BM25Retriever(vector_store=self.vector_store, search_store=self.search_store)
        self.hybrid_retriever = HybridRetriever(self.vector_retriever, self.bm25_retriever)
        self.query_expander = QueryExpander()
        self.reranker = CrossEncoderReranker(model_name=reranker_model, enabled=enable_reranker)


    def rebuild_bm25(self) -> None:
        """Expose rebuild function to rebuild BM25 indices on document updates."""
        self.bm25_retriever.rebuild()

    def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        filters: Optional[MetadataFilter] = None,
        auth_context: Optional[AuthorizationContext] = None,
        rerank_query: Optional[str] = None,
        rerank: bool = True
    ) -> RetrievalResponse:
        """
        Executes the search pipeline with deterministic authorization constraints.
        """
        # Ensure auth_context is attached to filters for pre-filtering
        if filters is None:
            if auth_context is not None:
                filters = MetadataFilter(auth_context=auth_context)
        elif auth_context is not None and filters.auth_context is None:
            filters.auth_context = auth_context

        v_top_k = config.VECTOR_TOP_K
        b_top_k = config.BM25_TOP_K
        h_top_k = config.HYBRID_TOP_K
        r_top_k = top_k or config.RERANK_TOP_K

        diagnostics = RetrievalDiagnostics()
        candidates: List[SearchResult] = []

        retrieval_start = time.perf_counter()

        from app.observability.tracing import tracer
        from app.observability.metrics import (
            retrieval_requests_total,
            retrieval_duration_seconds,
            retrieval_candidates_count,
            retrieval_empty_total,
            reranking_requests_total,
            reranking_duration_seconds,
            reranker_failures_total,
        )

        with tracer.start_span(f"retrieval.{self.strategy}") as span:
            if self.strategy == "vector":
                diagnostics.query_count = 1
                candidates = self.vector_retriever.retrieve(query, top_k=v_top_k, filters=filters)
                diagnostics.vector_candidate_count = len(candidates)

            elif self.strategy == "bm25":
                diagnostics.query_count = 1
                candidates = self.bm25_retriever.retrieve(query, top_k=b_top_k, filters=filters)
                diagnostics.bm25_candidate_count = len(candidates)

            elif self.strategy == "hybrid":
                diagnostics.query_count = 1
                # Run retrievals separately to log diagnostic numbers
                v_cand = self.vector_retriever.retrieve(query, top_k=v_top_k, filters=filters)
                b_cand = self.bm25_retriever.retrieve(query, top_k=b_top_k, filters=filters)
                diagnostics.vector_candidate_count = len(v_cand)
                diagnostics.bm25_candidate_count = len(b_cand)

                candidates = self.hybrid_retriever.retrieve(query, top_k=h_top_k, filters=filters)

            elif self.strategy == "fusion":
                # Generate alternate queries
                expanded_queries = self.query_expander.expand(query, n_queries=2)
                diagnostics.query_count = len(expanded_queries)

                ranked_lists: List[List[SearchResult]] = []
                total_v = 0
                total_b = 0

                # Query parallel retrievers for each query
                for q in expanded_queries:
                    v_list = self.vector_retriever.retrieve(q, top_k=v_top_k, filters=filters)
                    b_list = self.bm25_retriever.retrieve(q, top_k=b_top_k, filters=filters)
                    ranked_lists.append(v_list)
                    ranked_lists.append(b_list)
                    total_v += len(v_list)
                    total_b += len(b_list)

                diagnostics.vector_candidate_count = total_v
                diagnostics.bm25_candidate_count = total_b

                # Run rank merging RRF
                candidates = reciprocal_rank_fusion(ranked_lists, k=config.RRF_K)
                diagnostics.rrf_candidate_count = len(candidates)

            else:
                raise ValueError(f"Unsupported retrieval strategy: '{self.strategy}'")

        diagnostics.retrieval_latency_ms = (time.perf_counter() - retrieval_start) * 1000

        retrieval_requests_total.inc(labels={"strategy": self.strategy, "status": "success"})
        retrieval_duration_seconds.observe(diagnostics.retrieval_latency_ms / 1000.0, labels={"strategy": self.strategy})
        retrieval_candidates_count.observe(len(candidates), labels={"candidate_type": "raw"})

        if not candidates:
            retrieval_empty_total.inc(labels={"strategy": self.strategy})

        # De-duplicate chunks by chunk_id
        seen_chunks = set()
        deduped = []
        for c in candidates:
            if c.chunk_id not in seen_chunks:
                seen_chunks.add(c.chunk_id)
                deduped.append(c)

        # Authoritative PostgreSQL/Backend Authorization Filter BEFORE Reranking
        if auth_context is not None:
            deduped = [
                c for c in deduped
                if is_document_accessible(c.metadata, auth_context)
            ]

        retrieval_candidates_count.observe(len(deduped), labels={"candidate_type": "authorized"})
        diagnostics.reranker_candidate_count = len(deduped)

        if rerank:
            # Cross-Encoder Reranking stage
            rerank_start = time.perf_counter()
            query_for_rerank = rerank_query if rerank_query is not None else query
            with tracer.start_span("retrieval.rerank") as span:
                try:
                    reranked = self.reranker.rerank(query_for_rerank, deduped)
                    reranking_requests_total.inc(labels={"model": config.RERANKER_MODEL, "status": "success"})
                except Exception as exc:
                    reranker_failures_total.inc(labels={"error_class": exc.__class__.__name__})
                    if span:
                        span.set_status("ERROR")
                    reranked = deduped
            diagnostics.reranking_latency_ms = (time.perf_counter() - rerank_start) * 1000
            reranking_duration_seconds.observe(diagnostics.reranking_latency_ms / 1000.0, labels={"model": config.RERANKER_MODEL})
            
            # Slice to final top_k
            final_results = reranked[:r_top_k]
        else:
            final_results = deduped

        # Defense-in-depth: Ensure every returned candidate is strictly authorized
        if auth_context is not None:
            final_results = [
                c for c in final_results
                if is_document_accessible(c.metadata, auth_context)
            ]

        diagnostics.final_result_count = len(final_results)

        return RetrievalResponse(
            results=final_results,
            diagnostics=diagnostics
        )
