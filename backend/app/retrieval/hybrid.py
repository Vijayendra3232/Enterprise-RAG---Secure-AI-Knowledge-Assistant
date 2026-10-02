from typing import List, Optional, Dict, Any
from app import config
from app.retrieval.models import SearchResult
from app.retrieval.filters import MetadataFilter
from app.retrieval.vector import VectorRetriever
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.normalization import ScoreNormalizer, MinMaxNormalizer

class HybridRetriever:
    """
    Combines dense Vector Search and sparse BM25 Keyword Search.
    Scores from each source are normalized and merged using configurable weights.
    """
    def __init__(
        self,
        vector_retriever: VectorRetriever,
        bm25_retriever: BM25Retriever,
        vector_weight: float = config.VECTOR_WEIGHT,
        bm25_weight: float = config.BM25_WEIGHT,
        normalizer: Optional[ScoreNormalizer] = None
    ):
        self.vector_retriever = vector_retriever
        self.bm25_retriever = bm25_retriever
        self.vector_weight = vector_weight
        self.bm25_weight = bm25_weight
        self.normalizer = normalizer or MinMaxNormalizer()

    def retrieve(self, query: str, top_k: int = 10, filters: Optional[MetadataFilter] = None) -> List[SearchResult]:
        # 1. Fetch candidates from both search algorithms
        vec_results = self.vector_retriever.retrieve(query, top_k=top_k, filters=filters)
        bm25_results = self.bm25_retriever.retrieve(query, top_k=top_k, filters=filters)

        if not vec_results and not bm25_results:
            return []

        # 2. Normalize scores within their respective result sets
        norm_vec = self.normalizer.normalize(vec_results)
        norm_bm25 = self.normalizer.normalize(bm25_results)

        # Build mappings of chunk_id -> (result_obj, normalized_score)
        vec_map = {r.chunk_id: (r, r.score) for r in norm_vec}
        bm25_map = {r.chunk_id: (r, r.score) for r in norm_bm25}

        # Collect original scores for reporting
        orig_vec_scores = {r.chunk_id: r.score for r in vec_results}
        orig_bm25_scores = {r.chunk_id: r.score for r in bm25_results}

        # Gather all unique chunk IDs
        all_chunk_ids = set(vec_map.keys()).union(bm25_map.keys())

        merged_results = []
        for cid in all_chunk_ids:
            v_score_norm = vec_map.get(cid, (None, 0.0))[1]
            b_score_norm = bm25_map.get(cid, (None, 0.0))[1]

            # Weighted combination formula
            combined_score = (self.vector_weight * v_score_norm) + (self.bm25_weight * b_score_norm)

            # Extract base metadata / document details from whichever result has it
            orig_res = vec_map.get(cid)[0] if cid in vec_map else bm25_map.get(cid)[0]
            
            # Enrich metadata with diagnostic scores
            metadata = orig_res.metadata.copy() if orig_res.metadata else {}
            metadata.update({
                "retrieval_method": "hybrid",
                "vector_score": orig_vec_scores.get(cid, 0.0),
                "bm25_score": orig_bm25_scores.get(cid, 0.0),
                "vector_normalized_score": v_score_norm,
                "bm25_normalized_score": b_score_norm
            })

            merged_results.append(
                SearchResult(
                    chunk_id=orig_res.chunk_id,
                    document_id=orig_res.document_id,
                    content=orig_res.content,
                    score=combined_score,
                    source=orig_res.source,
                    page=orig_res.page,
                    metadata=metadata
                )
            )

        # Sort by combined score descending
        merged_results.sort(key=lambda x: x.score, reverse=True)
        return merged_results[:top_k]
