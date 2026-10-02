from typing import List, Optional
from pydantic import BaseModel, Field

class QueryAnalysis(BaseModel):
    """
    Structured analysis output representing query classification, intent,
    complexity, and proposed sub-queries or rewrites.
    """
    original_query: str
    intent: str = "unknown"  # factual, procedural, comparison, summarization, analytical, conversational, unknown
    complexity: str = "simple"  # simple, moderate, complex
    query_type: str = "single_hop"  # single_hop, multi_hop, multi_part, ambiguous, structured, conversational
    needs_rewriting: bool = False
    needs_decomposition: bool = False
    needs_expansion: bool = False
    rewritten_query: Optional[str] = None
    sub_queries: List[str] = Field(default_factory=list)
