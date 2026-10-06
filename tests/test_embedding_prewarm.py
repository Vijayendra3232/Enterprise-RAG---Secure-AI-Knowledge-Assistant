"""
test_embedding_prewarm.py — Targeted unit tests for embedding model pre-baking,
offline mode enforcement, and lifespan pre-warming behavior.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, os.path.abspath(_BACKEND))

from app.core import embeddings


class TestEmbeddingPrewarm(unittest.TestCase):
    """Test suite verifying embedding model offline mode and pre-warming."""

    def setUp(self):
        embeddings.load_embedding_model.cache_clear()

    def tearDown(self):
        embeddings.load_embedding_model.cache_clear()

    def test_load_embedding_model_offline_enforcement(self):
        """Verify RuntimeError is raised in offline mode if ONNX model files are missing."""
        embeddings.load_embedding_model.cache_clear()
        with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}), patch.object(
            embeddings.PureONNXEmbeddings, "_resolve_artifacts", side_effect=RuntimeError("Missing artifacts")
        ):
            with self.assertRaises(RuntimeError):
                embeddings.load_embedding_model("nonexistent_offline_model")
        embeddings.load_embedding_model.cache_clear()

    def test_load_embedding_model_production_enforcement(self):
        """Verify RuntimeError is raised in production mode if ONNX model files are missing."""
        embeddings.load_embedding_model.cache_clear()
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}), patch.object(
            embeddings.PureONNXEmbeddings, "_resolve_artifacts", side_effect=RuntimeError("Missing artifacts")
        ):
            with self.assertRaises(RuntimeError):
                embeddings.load_embedding_model("nonexistent_prod_model")
        embeddings.load_embedding_model.cache_clear()

    def test_lifespan_prewarm_failure_in_production(self):
        """Verify lifespan raises RuntimeError in production if model pre-warming fails when enabled."""
        from app.main import lifespan
        from app import config
        from fastapi import FastAPI

        app = FastAPI()
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}), patch.object(
            config, "PREWARM_EMBEDDING_MODEL", True
        ), patch("app.main.init_db"), patch(
            "app.storage.search.factory.get_search_store"
        ), patch.object(
            embeddings, "load_embedding_model", side_effect=RuntimeError("Model missing from cache")
        ):
            async def run_lifespan():
                async with lifespan(app):
                    pass

            import asyncio
            with self.assertRaises(RuntimeError):
                asyncio.run(run_lifespan())


if __name__ == "__main__":
    unittest.main()
