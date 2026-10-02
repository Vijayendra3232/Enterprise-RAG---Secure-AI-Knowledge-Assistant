from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

class CitationResponse(BaseModel):
    """
    Stabilized citation structure referencing verified retrieved chunks.
    """
    citation_id: str
    document_id: str
    chunk_id: str
    filename: str
    source: str
    page: int

class GroundingResponse(BaseModel):
    """
    Grounded verification status and indicators.
    """
    grounded: bool
    status: str  # SUPPORTED, PARTIALLY_SUPPORTED, UNSUPPORTED, INSUFFICIENT_EVIDENCE

class AnswerResponse(BaseModel):
    """
    Top-level grounded generation API response schema.
    """
    response: str
    citations: List[CitationResponse] = Field(default_factory=list)
    grounding: GroundingResponse
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
