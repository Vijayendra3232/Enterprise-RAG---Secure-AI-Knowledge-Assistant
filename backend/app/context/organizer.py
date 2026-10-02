from typing import List, Dict
from app import config
from app.retrieval.models import SearchResult

class ContextOrganizer:
    """
    Organizes final context chunks before submission to LLM.
    Enforces chunk and character budgets atomically, maintains relative ranking,
    caps single-document dominance, and formats source boundaries.
    """
    def __init__(
        self,
        max_chunks: int = config.MAX_CONTEXT_CHUNKS,
        max_chars: int = config.MAX_CONTEXT_CHARS,
        max_chunks_per_doc: int = 3
    ):
        self.max_chunks = max_chunks
        self.max_chars = max_chars
        self.max_chunks_per_doc = max_chunks_per_doc

    def organize(self, chunks: List[SearchResult]) -> List[SearchResult]:
        """
        Organizes chunks. Chunks are treated as atomic units. Stops immediately
        prior to exceeding character limits without cutting any chunk in the middle.
        """
        if not chunks:
            return []

        selected = []
        current_chars = 0
        doc_counts: Dict[str, int] = {}

        for c in chunks:
            # 1. Enforce max chunks limit
            if len(selected) >= self.max_chunks:
                break

            # 2. Limit single document dominance
            doc_id = c.document_id
            if doc_id:
                count = doc_counts.get(doc_id, 0)
                if count >= self.max_chunks_per_doc:
                    continue

            # 3. Atomic character budget checking
            # Header formatting overhead
            filename = c.metadata.get("filename") or "unknown"
            header = f"[Source: {filename}, Page {c.page}]\n"
            chunk_length = len(c.content) + len(header) + 2  # content length + header + boundary spacing

            if current_chars + chunk_length > self.max_chars:
                # Do NOT add this chunk or truncate it. Stop here.
                break

            selected.append(c)
            current_chars += chunk_length
            if doc_id:
                doc_counts[doc_id] = doc_counts.get(doc_id, 0) + 1

        return selected
