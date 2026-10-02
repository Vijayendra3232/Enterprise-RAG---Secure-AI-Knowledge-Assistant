import re
from typing import List, Optional
from app.grounding.models import Claim

class ClaimExtractor:
    """
    Extracts individual factual claims from the LLM generated answer.
    Supports structured JSON input and falls back to deterministic sentence-level extraction.
    """
    def extract_claims(self, text: str, structured_claims: Optional[List[dict]] = None) -> List[Claim]:
        if structured_claims:
            claims = []
            for item in structured_claims:
                text_content = str(item.get("text", "")).strip()
                evidence_ids = item.get("evidence_ids", [])
                if text_content:
                    # Clean tags if any are embedded in the text claim
                    clean_text = re.sub(r"\[C\d+\]", "", text_content).strip()
                    claims.append(Claim(text=clean_text, evidence_ids=evidence_ids))
            if claims:
                return claims

        # Fallback: Deterministic sentence-level extractor
        sentences = [s.strip() for s in re.split(r"(?<=\.|\?)\s+", text) if s.strip()]
        claims = []
        for s in sentences:
            # Detect citation tags in sentence
            tags = re.findall(r"\[(C\d+)\]", s)
            
            # Clean brackets to get pure assertion text
            clean_s = re.sub(r"\[C\d+\]", "", s).strip()
            # Clean spaces before trailing punctuation marks (e.g. 'word .' -> 'word.')
            clean_s = re.sub(r"\s+([.,!?])", r"\1", clean_s)
            clean_s = re.sub(r"\s+", " ", clean_s)
            if clean_s:
                claims.append(Claim(text=clean_s, evidence_ids=tags))

        return claims
