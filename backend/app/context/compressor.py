import re
from typing import List, Optional
from app import config
from app.retrieval.models import SearchResult

class ContextCompressor:
    """
    Safely compresses context chunks by removing irrelevant text while strictly
    preserving exceptions, conditions, dates, negations, qualifications, and numbers.
    Operates conservatively to protect citation metadata.
    """
    def __init__(self, enabled: bool = config.ENABLE_CONTEXT_COMPRESSION):
        self.enabled = enabled

        # Safe-preservation regex patterns
        self.exception_pattern = re.compile(
            r"\b(except|exception|unless|however|only if|provided|subject to|allow|permitted)\b",
            re.IGNORECASE
        )
        self.negation_pattern = re.compile(
            r"\b(not|never|no|cannot|must not|don\'t|prohibited|forbidden|restrict|restrictions)\b",
            re.IGNORECASE
        )
        self.number_pattern = re.compile(r"\b\d+\b")  # Matches any numeric details or dates
        self.qualification_pattern = re.compile(
            r"\b(year|month|week|day|hour|tenure|eligible|eligibility|policy|required|limit|maximum)\b",
            re.IGNORECASE
        )

    def compress(self, query: str, chunks: List[SearchResult]) -> List[SearchResult]:
        """
        Compresses a list of SearchResults. Returns original chunks if compression
        is disabled or if safe compression cannot be guaranteed.
        """
        if not self.enabled or not chunks:
            return chunks

        compressed_chunks = []
        for c in chunks:
            try:
                compressed_content = self._compress_chunk(query, c.content)
                
                # Create a copy and update content
                c_copy = c.model_copy()
                c_copy.content = compressed_content
                
                # Add diagnostics to metadata
                meta = c_copy.metadata.copy() if c_copy.metadata else {}
                meta["compressed"] = True
                meta["original_length"] = len(c.content)
                meta["compressed_length"] = len(compressed_content)
                c_copy.metadata = meta

                compressed_chunks.append(c_copy)
            except Exception as e:
                # Fail-safe: if any compression error occurs, fall back to the original chunk
                print(f"[ContextCompressor] Warning: chunk compression failed: {e}. Using original.")
                compressed_chunks.append(c)

        return compressed_chunks

    def _compress_chunk(self, query: str, content: str) -> str:
        # Extract query terms (alphanumeric, lowercase, ignoring short words)
        query_terms = [
            t.lower() for t in re.findall(r"\b\w{3,}\b", query)
            if t.lower() not in {"what", "how", "the", "and", "for", "with", "are", "our", "you"}
        ]

        # Split content into sentences
        # Splitting using sentence endings followed by space
        sentences = [s.strip() for s in re.split(r"(?<=\.|\?)\s+", content) if s.strip()]
        if not sentences:
            return content

        preserved_sentences = []
        for s in sentences:
            s_lower = s.lower()
            
            # ── Rule 1: Relevance to query terms ──────────────────────────────
            matches_query = any(term in s_lower for term in query_terms)
            
            # ── Rule 2: Exception & Condition checks ──────────────────────────
            has_exception = bool(self.exception_pattern.search(s_lower))
            
            # ── Rule 3: Negation & Restriction checks ─────────────────────────
            has_negation = bool(self.negation_pattern.search(s_lower))
            
            # ── Rule 4: Dates & Numbers checks ────────────────────────────────
            has_numbers = bool(self.number_pattern.search(s_lower))
            
            # ── Rule 5: Qualifications & Limits checks ────────────────────────
            has_qualification = bool(self.qualification_pattern.search(s_lower))

            # Keep the sentence if it matches query or contains policy restrictions/qualifications
            if matches_query or has_exception or has_negation or has_numbers or has_qualification:
                preserved_sentences.append(s)

        # Build compressed text
        compressed_text = " ".join(preserved_sentences)
        
        # If result is empty or too short, return the original content to ensure safety
        if not compressed_text.strip() or len(compressed_text) < 40:
            return content

        return compressed_text
