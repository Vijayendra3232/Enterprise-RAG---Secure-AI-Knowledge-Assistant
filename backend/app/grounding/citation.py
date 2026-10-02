import re
from typing import List, Dict, Tuple
from app.retrieval.models import SearchResult
from app.generation.models import CitationResponse

class CitationBuilder:
    """
    Transforms SearchResult objects from retrieval outputs into unique,
    stable citation IDs owned by the system.
    """
    def build_citations(self, chunks: List[SearchResult]) -> Tuple[List[CitationResponse], Dict[str, SearchResult]]:
        import os
        citations = []
        mapping = {}
        for idx, chunk in enumerate(chunks):
            citation_id = f"C{idx + 1}"
            filename = chunk.metadata.get("filename") or (os.path.basename(chunk.source) if chunk.source else "unknown")
            source = chunk.source or "unknown"
            page = chunk.page if chunk.page is not None else 1
            
            c_resp = CitationResponse(
                citation_id=citation_id,
                document_id=chunk.document_id or "unknown",
                chunk_id=chunk.chunk_id or "unknown",
                filename=filename,
                source=source,
                page=page
            )
            citations.append(c_resp)
            mapping[citation_id] = chunk

        return citations, mapping

class CitationValidator:
    """
    Validates LLM-supplied citation/evidence tags against the system's citation catalog.
    Detects and filters fabricated citation IDs (e.g., C999).
    """
    def __init__(self, citation_mapping: Dict[str, SearchResult]):
        self.mapping = citation_mapping

    def validate_citations(self, text: str, claim_evidence_ids: List[str]) -> Tuple[str, List[str]]:
        """
        Validates citation IDs. Removes invalid/fabricated citation tags from text
        and filters claim_evidence_ids.
        """
        # Find all pattern references e.g. [C1], [C2], [C999]
        citation_tags = re.findall(r"\[(C\d+)\]", text)
        valid_ids = []
        cleaned_text = text

        for tag in citation_tags:
            if tag in self.mapping:
                valid_ids.append(tag)
            else:
                # Fabricated tag! Remove the bracket citation [Cx] tag from the text
                cleaned_text = re.sub(rf"\[{tag}\]", "", cleaned_text)

        # Clean double spaces or clean spacing from tag removals
        cleaned_text = re.sub(r"\s+", " ", cleaned_text).strip()

        # Validate the claim's specific evidence mappings
        valid_evidence = [eid for eid in claim_evidence_ids if eid in self.mapping]

        return cleaned_text, valid_evidence
