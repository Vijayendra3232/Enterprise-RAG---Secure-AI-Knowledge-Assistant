"""
test_memory_optimization.py — Unit tests for memory optimizations:
lazy embedding model loading, default worker concurrency, micro-batching, and model instance singleton caching.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, os.path.abspath(_BACKEND))

from app import config
from app.core import embeddings


class TestMemoryOptimization(unittest.TestCase):
    """Test suite verifying low-memory startup and lazy loading behavior."""

    def setUp(self):
        embeddings.load_embedding_model.cache_clear()

    def tearDown(self):
        embeddings.load_embedding_model.cache_clear()

    def test_fastapi_startup_does_not_load_model_by_default(self):
        """Verify FastAPI lifespan startup does NOT eagerly load the embedding model by default."""
        from app.main import lifespan
        from fastapi import FastAPI
        import asyncio

        app = FastAPI()

        with patch.object(embeddings, "load_embedding_model") as mock_load:
            async def run_lifespan():
                async with lifespan(app):
                    pass

            asyncio.run(run_lifespan())
            mock_load.assert_not_called()

    def test_default_worker_concurrency_is_one(self):
        """Verify default production worker concurrency is 1."""
        self.assertEqual(config.WORKER_CONCURRENCY, 1)

    def test_default_embedding_batch_size_is_one(self):
        """Verify default production embedding batch size is 1."""
        self.assertEqual(config.EMBEDDING_BATCH_SIZE, 1)

    def test_lazy_embedding_model_cached_singleton(self):
        """Verify repeated calls to load_embedding_model return the same cached model instance."""
        embeddings.load_embedding_model.cache_clear()
        with patch("app.core.embeddings.HuggingFaceEmbeddings") as mock_hf:
            mock_instance = MagicMock()
            mock_hf.return_value = mock_instance

            model1 = embeddings.load_embedding_model("all-MiniLM-L6-v2_unique_cache_test")
            model2 = embeddings.load_embedding_model("all-MiniLM-L6-v2_unique_cache_test")

            self.assertIs(model1, model2)
            mock_hf.assert_called_once()
        embeddings.load_embedding_model.cache_clear()

    def test_embedding_vector_dimension_is_384(self):
        """Verify embedding dimension calculation for MiniLM remains 384."""
        embeddings.load_embedding_model.cache_clear()
        with patch("app.core.embeddings.HuggingFaceEmbeddings") as mock_hf:
            mock_instance = MagicMock()
            mock_client = MagicMock()
            mock_client.get_sentence_embedding_dimension.return_value = 384
            mock_instance.client = mock_client
            mock_hf.return_value = mock_instance

            dim = embeddings.get_embedding_dimension("all-MiniLM-L6-v2")
            self.assertEqual(dim, 384)
        embeddings.load_embedding_model.cache_clear()


if __name__ == "__main__":
    unittest.main()
