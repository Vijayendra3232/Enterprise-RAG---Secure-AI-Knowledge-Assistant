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
        """Verify local_files_only is set when HF_HUB_OFFLINE is set to 1."""
        embeddings.load_embedding_model.cache_clear()
        with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}), patch(
            "langchain_huggingface.HuggingFaceEmbeddings"
        ) as mock_hf, patch.object(embeddings, "HuggingFaceEmbeddings", new=mock_hf):
            mock_instance = MagicMock()
            mock_hf.return_value = mock_instance

            model = embeddings.load_embedding_model("offline_test_model_1_unique")

            mock_hf.assert_called_once()
            _, kwargs = mock_hf.call_args
            self.assertIn("model_kwargs", kwargs)
            self.assertTrue(kwargs["model_kwargs"].get("local_files_only"))
        embeddings.load_embedding_model.cache_clear()

    def test_load_embedding_model_production_enforcement(self):
        """Verify local_files_only is set in production environment."""
        embeddings.load_embedding_model.cache_clear()
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}), patch(
            "langchain_huggingface.HuggingFaceEmbeddings"
        ) as mock_hf, patch.object(embeddings, "HuggingFaceEmbeddings", new=mock_hf):
            mock_instance = MagicMock()
            mock_hf.return_value = mock_instance

            model = embeddings.load_embedding_model("prod_test_model_2_unique")

            mock_hf.assert_called_once()
            _, kwargs = mock_hf.call_args
            self.assertIn("model_kwargs", kwargs)
            self.assertTrue(kwargs["model_kwargs"].get("local_files_only"))
        embeddings.load_embedding_model.cache_clear()

    def test_lifespan_prewarm_failure_in_production(self):
        """Verify lifespan raises RuntimeError in production if model pre-warming fails."""
        from app.main import lifespan
        from fastapi import FastAPI

        app = FastAPI()
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}), patch.object(
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
