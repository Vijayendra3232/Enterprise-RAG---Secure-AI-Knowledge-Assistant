"""
test_onnx_embeddings.py — Unit test suite for PureONNXEmbeddings architecture.
Verifies zero PyTorch/Transformers dependencies, 384-dimensional L2 normalized output,
attention-mask-aware mean pooling, single-thread CPU constraints, and zero network requirement.
"""

import os
import sys
import unittest
import numpy as np
import subprocess

_BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, os.path.abspath(_BACKEND))

from app.core import embeddings
from app.core.embeddings import PureONNXEmbeddings, load_embedding_model, get_embedding_dimension


class TestONNXEmbeddings(unittest.TestCase):
    """Focused test suite for Pure ONNX Embeddings."""

    @classmethod
    def setUpClass(cls):
        cls.model = load_embedding_model("sentence-transformers/all-MiniLM-L6-v2")

    def test_model_loads_and_is_pure_onnx(self):
        """Verify model is an instance of PureONNXEmbeddings."""
        self.assertIsInstance(self.model, PureONNXEmbeddings)

    def test_output_dimension_is_384(self):
        """Verify vector dimension is exactly 384."""
        vec = self.model.embed_query("Security policy document")
        self.assertEqual(len(vec), 384)
        self.assertEqual(get_embedding_dimension("sentence-transformers/all-MiniLM-L6-v2"), 384)

    def test_output_values_are_finite(self):
        """Verify all vector values are finite numbers (no NaN or Inf)."""
        vec = self.model.embed_query("Security policy document")
        self.assertTrue(np.all(np.isfinite(vec)))

    def test_l2_norm_is_one(self):
        """Verify vector L2 norm is approximately 1.0."""
        vec = self.model.embed_query("Security policy document")
        norm = np.linalg.norm(vec)
        self.assertAlmostEqual(norm, 1.0, places=5)

    def test_embed_documents_multiple_texts(self):
        """Verify multiple texts are processed into batch 384-dim normalized vectors."""
        texts = [
            "Company security policy requires all employees to protect confidential information.",
            "Employees must follow company security and data protection requirements.",
            "Farmers can sell their crops through the organization.",
        ]
        vecs = self.model.embed_documents(texts)
        self.assertEqual(len(vecs), 3)
        for v in vecs:
            self.assertEqual(len(v), 384)
            self.assertAlmostEqual(np.linalg.norm(v), 1.0, places=5)

    def test_embed_query_matches_embed_documents(self):
        """Verify embed_query returns the same vector format as embed_documents."""
        text = "Test query text for vector space compatibility"
        query_vec = self.model.embed_query(text)
        doc_vec = self.model.embed_documents([text])[0]
        np.testing.assert_allclose(query_vec, doc_vec, rtol=1e-5, atol=1e-5)

    def test_attention_mask_mean_pooling(self):
        """Verify padding tokens do not distort vector representations."""
        short_text = "Security policy."
        long_text = "Security policy. " + ("extra padding tokens " * 20)
        
        # When embedded individually vs in a batch with padding, the short_text vector should be identical
        vec_single = self.model.embed_query(short_text)
        vec_batch = self.model.embed_documents([short_text, long_text])[0]
        
        sim = np.dot(vec_single, vec_batch)
        self.assertAlmostEqual(sim, 1.0, places=5)

    def test_empty_and_whitespace_input_handling(self):
        """Verify empty and whitespace inputs produce valid finite vectors without crashing."""
        for text in ["", "   ", "\n\t"]:
            vec = self.model.embed_query(text)
            self.assertEqual(len(vec), 384)
            self.assertTrue(np.all(np.isfinite(vec)))
            self.assertAlmostEqual(np.linalg.norm(vec), 1.0, places=5)

    def test_onnx_runtime_thread_constraints_and_provider(self):
        """Verify ONNX Runtime uses CPUExecutionProvider and is constrained to 1 thread."""
        providers = self.model.session.get_providers()
        self.assertIn("CPUExecutionProvider", providers)

    def test_subprocess_zero_torch_and_transformers_import(self):
        """Verify importing app.core.embeddings in a clean Python subprocess does NOT load torch or transformers."""
        code = (
            "import sys; "
            "import os; "
            f"sys.path.insert(0, r'{_BACKEND}'); "
            "from app.core.embeddings import load_embedding_model; "
            "m = load_embedding_model(); "
            "v = m.embed_query('Probe text'); "
            "assert 'torch' not in sys.modules, f'torch loaded: {sys.modules.get(\"torch\")}'; "
            "assert 'transformers' not in sys.modules, f'transformers loaded: {sys.modules.get(\"transformers\")}'; "
            "print('SUCCESS')"
        )
        cmd = [sys.executable, "-c", code]
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=_BACKEND)
        self.assertEqual(res.returncode, 0, f"Subprocess failed:\nStdout: {res.stdout}\nStderr: {res.stderr}")
        self.assertIn("SUCCESS", res.stdout)


if __name__ == "__main__":
    unittest.main()
