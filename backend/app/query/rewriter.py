from app import config
from app.query.models import QueryAnalysis

class QueryRewriter:
    """
    Resolves vague or conversational query terms into search-optimized queries.
    Uses pre-computed results from QueryAnalysis to prevent redundant LLM calls.
    """
    def __init__(self, enabled: bool = config.ENABLE_QUERY_REWRITING):
        self.enabled = enabled

    def rewrite(self, analysis: QueryAnalysis) -> str:
        """
        Rewrite query if rewriting is enabled and the query analysis suggests it.
        Otherwise, returns original query safely.
        """
        if not self.enabled:
            return analysis.original_query

        if analysis.needs_rewriting and analysis.rewritten_query:
            rewritten = analysis.rewritten_query.strip()
            if rewritten:
                return rewritten

        return analysis.original_query
