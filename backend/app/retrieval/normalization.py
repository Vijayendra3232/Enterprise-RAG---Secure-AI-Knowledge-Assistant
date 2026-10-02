from abc import ABC, abstractmethod
from typing import List
from app.retrieval.models import SearchResult

class ScoreNormalizer(ABC):
    """
    Abstract base class for normalizing document retrieval scores.
    Allows swapping normalization algorithms (Min-Max scaling, Z-score, etc.)
    """
    @abstractmethod
    def normalize(self, results: List[SearchResult]) -> List[SearchResult]:
        """
        Normalize score values in a list of SearchResults to the range [0, 1].
        Returns a new list of SearchResults with updated score fields.
        """
        pass


class MinMaxNormalizer(ScoreNormalizer):
    """
    Normalizes scores using Min-Max scaling:
        scaled = (x - min) / (max - min)
    """
    def normalize(self, results: List[SearchResult]) -> List[SearchResult]:
        if not results:
            return []

        scores = [r.score for r in results]
        min_val = min(scores)
        max_val = max(scores)
        diff = max_val - min_val

        normalized = []
        for r in results:
            # Model copy to prevent side effects
            r_copy = r.model_copy()
            if diff > 1e-9:
                r_copy.score = (r.score - min_val) / diff
            else:
                r_copy.score = 1.0
            normalized.append(r_copy)

        return normalized
