from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from app import config
from app.retrieval.models import SearchResult

# Exposed for unittest mocking without importing sentence_transformers at module load
CrossEncoder = None

class RerankerInterface(ABC):
    """
    Unified abstract interface for reranking candidate search results.
    """
    @abstractmethod
    def rerank(self, query: str, candidates: List[SearchResult]) -> List[SearchResult]:
        """Rerank candidates based on semantic relevance to query."""
        pass


class NoOpReranker(RerankerInterface):
    """
    A baseline retriever that returns candidates in their original order.
    """
    def rerank(self, query: str, candidates: List[SearchResult]) -> List[SearchResult]:
        return candidates


# Global cache to ensure model is only loaded once in the application context
_CROSS_ENCODER_CACHE: Dict[str, Any] = {}

def get_cross_encoder(model_name: str) -> Any:
    """Gets or loads a CrossEncoder model instance singleton."""
    global _CROSS_ENCODER_CACHE, CrossEncoder
    if model_name not in _CROSS_ENCODER_CACHE:
        if CrossEncoder is None:
            from sentence_transformers import CrossEncoder as STCrossEncoder
            CrossEncoder = STCrossEncoder
        print(f"[Reranker] Loading CrossEncoder model '{model_name}'...")
        # Load sentence-transformers CrossEncoder
        _CROSS_ENCODER_CACHE[model_name] = CrossEncoder(model_name)
    return _CROSS_ENCODER_CACHE[model_name]


class CrossEncoderReranker(RerankerInterface):
    """
    Reranks documents using a Cross-Encoder transformer model.
    """
    def __init__(self, model_name: str = config.RERANKER_MODEL, enabled: bool = config.ENABLE_RERANKER):
        self.model_name = model_name
        self.enabled = enabled
        self._model = None

    def _get_model(self) -> Optional[Any]:
        if not self.enabled:
            return None
        if self._model is None:
            try:
                self._model = get_cross_encoder(self.model_name)
            except Exception as e:
                print(f"[CrossEncoderReranker] Warning: failed to load cross-encoder model: {e}")
                self.enabled = False
        return self._model

    def rerank(self, query: str, candidates: List[SearchResult]) -> List[SearchResult]:
        if not self.enabled or not candidates:
            return candidates

        model = self._get_model()
        if not model:
            return candidates

        # Format input pairs
        pairs = [[query, c.content] for c in candidates]
        
        # Compute scores
        scores = model.predict(pairs)

        reranked = []
        for i, c in enumerate(candidates):
            c_copy = c.model_copy()
            score_val = float(scores[i])
            c_copy.score = score_val

            meta = c_copy.metadata.copy() if c_copy.metadata else {}
            meta["reranker_score"] = score_val
            meta["reranked"] = True
            c_copy.metadata = meta

            reranked.append(c_copy)

        # Sort descending by cross-encoder score
        reranked.sort(key=lambda x: x.score, reverse=True)
        return reranked
