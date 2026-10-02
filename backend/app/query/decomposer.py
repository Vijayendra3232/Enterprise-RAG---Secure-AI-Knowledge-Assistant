from typing import List
from app import config
from app.query.models import QueryAnalysis

class QueryDecomposer:
    """
    Decomposes complex or multi-part questions into individual, search-friendly sub-queries.
    Uses pre-computed sub-queries from QueryAnalysis to prevent redundant LLM calls.
    """
    def __init__(
        self,
        enabled: bool = config.ENABLE_QUERY_DECOMPOSITION,
        max_sub_queries: int = config.MAX_SUB_QUERIES
    ):
        self.enabled = enabled
        self.max_sub_queries = max_sub_queries

    def decompose(self, analysis: QueryAnalysis) -> List[str]:
        """
        Decomposes query into list of queries if enabled and suggested by analysis.
        Otherwise returns list containing the original query.
        """
        if not self.enabled:
            return [analysis.original_query]

        if analysis.needs_decomposition and analysis.sub_queries:
            # Clean and filter sub-queries
            valid_subs = [q.strip() for q in analysis.sub_queries if q.strip()]
            if valid_subs:
                # Cap list size to MAX_SUB_QUERIES
                return valid_subs[:self.max_sub_queries]

        return [analysis.original_query]
