"""
test_retrieval.py — Unit and integration tests for the Advanced Retrieval Engine.

Tests cover:
    - Standardized SearchResult format and metadata preservation
    - Vector retrieval with similarity score conversion
    - BM25 retrieval tokenization, ranking, and corpus index rebuild
    - Score normalization (Min-Max)
    - Hybrid retrieval combining vector and BM25 scores
    - Reciprocal Rank Fusion (RRF) rank merging over multiple ranked lists
    - Cross-Encoder reranking, optional state, and model singleton caching
    - Metadata pre/post filtering
    - Configurable top-k parameter settings
    - Diagnostics tracking (candidate counts, query count, latencies)
    - Duplicate chunk removal
    - Safe execution with empty result sets

All tests are completely offline-compatible via mocks.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add backend directory to sys.path so we can import 'app'
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

# --- Mocks ---

class MockEmbeddings:
    def embed_documents(self, texts):
        return [[0.1] * 384 for _ in texts]
    def embed_query(self, text):
        return [0.1] * 384

class MockLLM:
    def __init__(self, *args, **kwargs):
        pass
    def generate_response(self, prompt):
        if "semantically equivalent search queries" in prompt:
            return '["alternate query 1", "alternate query 2"]'
        return "Mock LLM Response"

cross_encoder_load_count = 0

class MockCrossEncoder:
    def __init__(self, model_name, *args, **kwargs):
        global cross_encoder_load_count
        cross_encoder_load_count += 1
        self.model_name = model_name

    def predict(self, pairs):
        # Simply return scores based on content length for deterministic ranking
        return [float(len(p[1])) for p in pairs]

import app.retrieval.reranker

# Now import modules to test
from langchain_core.documents import Document
from app.retrieval.models import SearchResult, RetrievalDiagnostics, RetrievalResponse
from app.retrieval.filters import MetadataFilter
from app.retrieval.vector import VectorRetriever, VectorStoreInterface
from app.retrieval.bm25 import BM25Retriever, InMemoryBM25Index
from app.retrieval.normalization import MinMaxNormalizer
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.fusion import reciprocal_rank_fusion, QueryExpander
from app.retrieval.reranker import CrossEncoderReranker, get_cross_encoder
from app.retrieval.pipeline import RetrievalPipeline
from app import config


# Mock Vector Store implementation for isolated retrieval testing
class DummyVectorStore(VectorStoreInterface):
    def __init__(self):
        self.docs = [
            Document(
                page_content="The policy for paid vacation time is 15 days.",
                metadata={"chunk_id": "c1", "document_id": "doc1", "source": "hr.md", "page": 1, "filename": "hr.md", "file_type": "md"}
            ),
            Document(
                page_content="Sabbatical leaves can be requested after 5 years of tenure.",
                metadata={"chunk_id": "c2", "document_id": "doc2", "source": "benefits.md", "page": 3, "filename": "benefits.md", "file_type": "md"}
            ),
            Document(
                page_content="Employees receive 5 paid sick days per calendar year.",
                metadata={"chunk_id": "c3", "document_id": "doc3", "source": "hr.md", "page": 2, "filename": "hr.md", "file_type": "md"}
            )
        ]
        
    def similarity_search_with_score(self, query: str, k: int, filter=None):
        filtered = self.docs
        if filter:
            # Simple metadata filter match helper for tests
            if "filename" in filter:
                filtered = [d for d in filtered if d.metadata.get("filename") == filter["filename"]]
            elif "$and" in filter:
                for cond in filter["$and"]:
                    for field, val in cond.items():
                        filtered = [d for d in filtered if d.metadata.get(field) == val]
        
        # Return with mock L2 distances (smaller is closer)
        results = []
        for idx, doc in enumerate(filtered[:k]):
            results.append((doc, float(idx * 0.5)))
        return results

    @property
    def vectordb(self):
        # Mock Chroma .get() method used by BM25Retriever rebuilding
        class MockChromaDB:
            def __init__(self, parent):
                self.parent = parent
            def get(self):
                return {
                    "ids": [doc.metadata["chunk_id"] for doc in self.parent.docs],
                    "documents": [doc.page_content for doc in self.parent.docs],
                    "metadatas": [doc.metadata for doc in self.parent.docs]
                }
        return MockChromaDB(self)


# --- Test Cases ---

class TestAdvancedRetrieval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patcher_embeddings = patch("app.core.embeddings.load_embedding_model", return_value=MockEmbeddings())
        cls.patcher_embeddings.start()
        cls.patcher_llm = patch("app.core.llm.LLM", return_value=MockLLM())
        cls.patcher_llm.start()
        cls.patcher_ce = patch("app.retrieval.reranker.CrossEncoder", side_effect=MockCrossEncoder)
        cls.patcher_ce.start()

    @classmethod
    def tearDownClass(cls):
        cls.patcher_embeddings.stop()
        cls.patcher_llm.stop()
        cls.patcher_ce.stop()
    def setUp(self):
        self.vector_store = DummyVectorStore()
        self.vector_retriever = VectorRetriever(self.vector_store)
        self.bm25_retriever = BM25Retriever(self.vector_store)

    # ═══════════════════════════════════════════════════════════════════════════
    # 1. Vector Retrieval
    # ═══════════════════════════════════════════════════════════════════════════
    def test_vector_retrieval_standardized_format(self):
        res = self.vector_retriever.retrieve("vacation policy", top_k=2)
        self.assertEqual(len(res), 2)
        
        first = res[0]
        self.assertIsInstance(first, SearchResult)
        self.assertEqual(first.chunk_id, "c1")
        self.assertEqual(first.document_id, "doc1")
        self.assertEqual(first.source, "hr.md")
        self.assertEqual(first.page, 1)
        # Similarity score conversion: 1.0 / (1.0 + 0.0) = 1.0
        self.assertAlmostEqual(first.score, 1.0)
        self.assertIn("filename", first.metadata)

    # ═══════════════════════════════════════════════════════════════════════════
    # 2. BM25 Retrieval
    # ═══════════════════════════════════════════════════════════════════════════
    def test_bm25_tokenization_and_ranking(self):
        res = self.bm25_retriever.retrieve("sabbatical leaves", top_k=1)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].chunk_id, "c2")
        self.assertIn("Sabbatical", res[0].content)

    def test_bm25_index_refresh(self):
        # Add new document directly to vector store mock docs
        new_doc = Document(
            page_content="New policy details about remote working setups.",
            metadata={"chunk_id": "c4", "document_id": "doc4", "source": "remote.md", "page": 1, "filename": "remote.md", "file_type": "md"}
        )
        self.vector_store.docs.append(new_doc)
        
        # Verify old index doesn't have it
        res_before = self.bm25_retriever.retrieve("remote working", top_k=1)
        self.assertTrue(len(res_before) == 0 or res_before[0].chunk_id != "c4")
        
        # Trigger index rebuild
        self.bm25_retriever.rebuild()
        
        # Verify it can be retrieved now
        res_after = self.bm25_retriever.retrieve("remote working", top_k=1)
        self.assertEqual(len(res_after), 1)
        self.assertEqual(res_after[0].chunk_id, "c4")
        
        # Cleanup
        self.vector_store.docs.remove(new_doc)

    # ═══════════════════════════════════════════════════════════════════════════
    # 3. Score Normalization & Hybrid Retrieval
    # ═══════════════════════════════════════════════════════════════════════════
    def test_min_max_normalization(self):
        normalizer = MinMaxNormalizer()
        results = [
            SearchResult(chunk_id="1", document_id="a", content="x", score=10.0, source="a", page=1),
            SearchResult(chunk_id="2", document_id="b", content="y", score=5.0, source="b", page=1),
            SearchResult(chunk_id="3", document_id="c", content="z", score=0.0, source="c", page=1),
        ]
        norm = normalizer.normalize(results)
        self.assertEqual(norm[0].score, 1.0)
        self.assertEqual(norm[1].score, 0.5)
        self.assertEqual(norm[2].score, 0.0)

    def test_hybrid_retrieval_combines_vector_and_bm25(self):
        hybrid = HybridRetriever(
            vector_retriever=self.vector_retriever,
            bm25_retriever=self.bm25_retriever,
            vector_weight=0.6,
            bm25_weight=0.4
        )
        res = hybrid.retrieve("sick days policy", top_k=2)
        self.assertGreater(len(res), 0)
        
        first = res[0]
        # Metadata check for hybrid properties
        self.assertIn("retrieval_method", first.metadata)
        self.assertIn("vector_score", first.metadata)
        self.assertIn("bm25_score", first.metadata)

    # ═══════════════════════════════════════════════════════════════════════════
    # 4. Reciprocal Rank Fusion (RRF)
    # ═══════════════════════════════════════════════════════════════════════════
    def test_rrf_with_multiple_ranked_lists(self):
        # Setup two ranked lists representing results from Q1 and Q2
        list_a = [
            SearchResult(chunk_id="c1", document_id="d1", content="vacation", score=0.9, source="hr.md", page=1),
            SearchResult(chunk_id="c2", document_id="d2", content="sabbatical", score=0.8, source="benefits.md", page=3)
        ]
        list_b = [
            SearchResult(chunk_id="c2", document_id="d2", content="sabbatical", score=0.95, source="benefits.md", page=3),
            SearchResult(chunk_id="c3", document_id="d3", content="sick days", score=0.7, source="hr.md", page=2)
        ]
        
        fused = reciprocal_rank_fusion([list_a, list_b], k=60)
        
        # Document c2 ranked second in list A and first in list B:
        # Score_c2 = 1/(60+2) + 1/(60+1) = 1/62 + 1/61 = 0.016129 + 0.016393 = 0.032522
        # Document c1 ranked first in list A:
        # Score_c1 = 1/(60+1) = 1/61 = 0.016393
        self.assertEqual(fused[0].chunk_id, "c2")
        self.assertEqual(fused[1].chunk_id, "c1")
        self.assertEqual(fused[2].chunk_id, "c3")

    def test_rrf_not_operating_on_single_hybrid_list(self):
        # We ensure RRF correctly accepts a List of List of SearchResults representing distinct outputs
        # from multiple retrieval models/queries rather than just sorting a single list in place.
        list_single = [
            SearchResult(chunk_id="c1", document_id="d1", content="txt", score=0.9, source="a", page=1)
        ]
        fused = reciprocal_rank_fusion([list_single], k=60)
        self.assertEqual(fused[0].score, 1.0 / (60 + 1))

    # ═══════════════════════════════════════════════════════════════════════════
    # 5. Reranker & Cache Singletons
    # ═══════════════════════════════════════════════════════════════════════════
    def test_reranker_singleton_caching(self):
        global cross_encoder_load_count
        from app.retrieval.reranker import _CROSS_ENCODER_CACHE
        _CROSS_ENCODER_CACHE.clear()
        
        start_count = cross_encoder_load_count
        
        # Fetching rerankers multiple times should trigger only a single model load
        enc1 = get_cross_encoder("ms-marco-MiniLM-L-6-v2")
        enc2 = get_cross_encoder("ms-marco-MiniLM-L-6-v2")
        
        self.assertEqual(enc1, enc2)
        self.assertEqual(cross_encoder_load_count, start_count + 1)

    def test_disabled_reranker(self):
        reranker = CrossEncoderReranker(enabled=False)
        candidates = [
            SearchResult(chunk_id="c1", document_id="d1", content="abc", score=0.5, source="a", page=1),
            SearchResult(chunk_id="c2", document_id="d2", content="xyz", score=0.9, source="b", page=1),
        ]
        # Should preserve original ranking
        reranked = reranker.rerank("query", candidates)
        self.assertEqual(reranked[0].chunk_id, "c1")
        self.assertEqual(reranked[1].chunk_id, "c2")

    # ═══════════════════════════════════════════════════════════════════════════
    # 6. Metadata Filtering
    # ═══════════════════════════════════════════════════════════════════════════
    def test_metadata_filtering(self):
        # Filter for file benefits.md
        filters = MetadataFilter(filename="benefits.md")
        res = self.vector_retriever.retrieve("leaves", top_k=5, filters=filters)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].metadata.get("filename"), "benefits.md")
        self.assertEqual(res[0].chunk_id, "c2")

    # ═══════════════════════════════════════════════════════════════════════════
    # 7. Configurable Top-k Parameters
    # ═══════════════════════════════════════════════════════════════════════════
    def test_configurable_top_k(self):
        res = self.vector_retriever.retrieve("vacation policy", top_k=1)
        self.assertEqual(len(res), 1)

    # ═══════════════════════════════════════════════════════════════════════════
    # 8. Diagnostics, Pipelines & Regression Checks
    # ═══════════════════════════════════════════════════════════════════════════
    def test_retrieval_pipeline_fusion_flow(self):
        pipeline = RetrievalPipeline(self.vector_store, strategy="fusion")
        response = pipeline.search("vacation leaves policy", top_k=2)
        
        # Verify result content and standard models
        self.assertIsInstance(response, RetrievalResponse)
        self.assertEqual(len(response.results), 2)
        
        # Verify metadata preserves through vector -> RRF -> reranker
        for r in response.results:
            self.assertIsNotNone(r.chunk_id)
            self.assertIsNotNone(r.document_id)
            self.assertIsNotNone(r.source)
            self.assertIsNotNone(r.page)
            self.assertIn("reranked", r.metadata)

        # Verify diagnostics tracking
        diag = response.diagnostics
        self.assertGreater(diag.query_count, 0)
        self.assertGreater(diag.vector_candidate_count, 0)
        self.assertGreater(diag.bm25_candidate_count, 0)
        self.assertGreater(diag.rrf_candidate_count, 0)
        self.assertGreater(diag.reranker_candidate_count, 0)
        self.assertEqual(diag.final_result_count, 2)
        self.assertGreater(diag.retrieval_latency_ms, 0.0)
        self.assertGreater(diag.reranking_latency_ms, 0.0)

    def test_empty_retrieval_safety(self):
        # Force empty retrieval matches
        filters = MetadataFilter(filename="nonexistent.md")
        pipeline = RetrievalPipeline(self.vector_store, strategy="fusion")
        response = pipeline.search("test", top_k=5, filters=filters)
        self.assertEqual(len(response.results), 0)
        self.assertEqual(response.diagnostics.final_result_count, 0)

    def test_duplicate_chunks_removed(self):
        # We simulate returning duplicates from multiple sources
        # Duplicate chunks are removed based on chunk_id in the pipeline
        pipeline = RetrievalPipeline(self.vector_store, strategy="vector")
        
        # Mock retrieval returning duplicates
        with patch.object(
            pipeline.vector_retriever, "retrieve",
            return_value=[
                SearchResult(chunk_id="c1", document_id="d1", content="x", score=1.0, source="a", page=1),
                SearchResult(chunk_id="c1", document_id="d1", content="x", score=0.9, source="a", page=1),
                SearchResult(chunk_id="c2", document_id="d2", content="y", score=0.8, source="b", page=1)
            ]
        ):
            response = pipeline.search("test", top_k=5)
            self.assertEqual(len(response.results), 2)
            self.assertEqual(response.results[0].chunk_id, "c1")
            self.assertEqual(response.results[1].chunk_id, "c2")


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        # Cleanup mock patches
        patcher_embeddings.stop()
        patcher_llm.stop()
        patcher_ce.stop()
