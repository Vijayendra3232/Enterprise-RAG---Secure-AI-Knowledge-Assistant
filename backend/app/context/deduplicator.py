import hashlib
from typing import List
from app.retrieval.models import SearchResult

class ContextDeduplicator:
    """
    De-duplicates retrieved context chunks to ensure uniqueness.
    Identifies duplicates using chunk_id, falling back to a stable MD5 hash of text content.
    Retains the chunk with the highest similarity score.
    """
    def deduplicate(self, chunks: List[SearchResult]) -> List[SearchResult]:
        if not chunks:
            return []

        # Find the highest scoring occurrence for each unique key
        best_records = {}
        for c in chunks:
            key = c.chunk_id
            if not key or key == "unknown":
                content_bytes = c.content.encode("utf-8", errors="replace")
                key = hashlib.md5(content_bytes).hexdigest()

            if key in best_records:
                if c.score > best_records[key].score:
                    best_records[key] = c
            else:
                best_records[key] = c

        # Re-build list preserving original relative rank order
        deduped = []
        yielded = set()
        for c in chunks:
            key = c.chunk_id
            if not key or key == "unknown":
                content_bytes = c.content.encode("utf-8", errors="replace")
                key = hashlib.md5(content_bytes).hexdigest()

            if key not in yielded:
                deduped.append(best_records[key])
                yielded.add(key)

        return deduped
