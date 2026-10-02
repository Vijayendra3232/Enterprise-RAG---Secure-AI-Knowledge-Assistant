from typing import List
from pydantic import BaseModel, Field

class Claim(BaseModel):
    """
    Factual assertion claim parsed from drafted generation.
    """
    text: str
    evidence_ids: List[str] = Field(default_factory=list)
