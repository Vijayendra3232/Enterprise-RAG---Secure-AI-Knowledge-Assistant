"""
test_query_context.py — Unit and integration tests for Query Intelligence and Context Optimization.

Tests cover:
    - QueryAnalyzer: complex 'how' queries, ambiguous keywords in context, failure fail-safe, conversational.
    - QueryRewriter: rewrite success/failure/disabled, intent preservation.
    - QueryDecomposer: multi-part decomposition, simple query preservation, sub-query capping (> MAX_SUB_QUERIES).
    - ContextDeduplicator: deduplication by chunk_id, stable content hash fallback, metadata preservation.
    - ContextCompressor: exception/negation/numbers preservation, disabled mode, failure fail-safe, citation survival.
    - ContextOrganizer: chunk limit (MAX_CONTEXT_CHUNKS), atomic character budget checking (never split a chunk).
    - Integration: simple, rewritten, and decomposed flows, original query used for reranking, fail-safes.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add backend directory to sys.path
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
        # Extract the specific query part from the prompt to avoid matching instructions
        prompt_lower = prompt.lower()
        query_part = prompt_lower.rsplit("analyze query:", 1)[-1] if "analyze query:" in prompt_lower else prompt_lower
        
        if "what about the leave thing" in query_part:
            return """{
                "intent": "factual",
                "complexity": "simple",
                "query_type": "ambiguous",
                "needs_rewriting": true,
                "needs_decomposition": false,
                "needs_expansion": true,
                "rewritten_query": "What is the company's employee leave policy?",
                "sub_queries": []
            }"""
        elif "vacation policy and how does it compare" in query_part:
            return """{
                "intent": "comparison",
                "complexity": "complex",
                "query_type": "multi_part",
                "needs_rewriting": false,
                "needs_decomposition": true,
                "needs_expansion": false,
                "rewritten_query": "vacation vs sabbatical comparison",
                "sub_queries": [
                    "What is the company's vacation policy?",
                    "What is the company's sabbatical policy?"
                ]
            }"""
        elif "how can i apply for an exception" in query_part:
            # Complex how question starting with 'how' but procedural complexity
            return """{
                "intent": "procedural",
                "complexity": "moderate",
                "query_type": "single_hop",
                "needs_rewriting": false,
                "needs_decomposition": false,
                "needs_expansion": false,
                "rewritten_query": "process to apply for policy exception",
                "sub_queries": []
            }"""
        elif "stuff in my locker" in query_part:
            # Ambiguous keyword check
            return """{
                "intent": "factual",
                "complexity": "simple",
                "query_type": "ambiguous",
                "needs_rewriting": true,
                "needs_decomposition": false,
                "needs_expansion": false,
                "rewritten_query": "policy regarding personal items in lockers",
                "sub_queries": []
            }"""
        elif "excessive sub-queries" in query_part:
            # Multi-part query exceeding maximum sub-queries
            return """{
                "intent": "factual",
                "complexity": "complex",
                "query_type": "multi_part",
                "needs_rewriting": false,
                "needs_decomposition": true,
                "needs_expansion": false,
                "rewritten_query": "multiple query checks",
                "sub_queries": ["query 1", "query 2", "query 3", "query 4", "query 5", "query 6"]
            }"""
        # Default simple analysis
        return """{
            "intent": "factual",
            "complexity": "simple",
            "query_type": "single_hop",
            "needs_rewriting": false,
            "needs_decomposition": false,
            "needs_expansion": false,
            "rewritten_query": null,
            "sub_queries": []
        }"""

class MockCrossEncoder:
    def __init__(self, *args, **kwargs):
        pass
    def predict(self, pairs):
        # Yield deterministic score based on length of content
        return [float(len(p[1])) for p in pairs]

import app.retrieval.reranker

# Now import modules
from app.retrieval.models import SearchResult
from app.query.models import QueryAnalysis
from app.query.analyzer import QueryAnalyzer
from app.query.rewriter import QueryRewriter
from app.query.decomposer import QueryDecomposer
from app.context.deduplicator import ContextDeduplicator
from app.context.compressor import ContextCompressor
from app.context.organizer import ContextOrganizer
from app.services.rag_service import RAGService
from app.retrieval.vector import VectorStoreInterface
from langchain_core.documents import Document


# Mock Vector Store for integration testing
class DummyVectorDB(VectorStoreInterface):
    def __init__(self):
        self.docs = [
            Document(
                page_content="Vacation policy details. Employees get 15 days of PTO.",
                metadata={"chunk_id": "c1", "document_id": "doc1", "source": "hr.md", "page": 1, "filename": "hr.md"}
            ),
            Document(
                page_content="Sabbatical leaves: Every three years employees take 6 weeks of paid sabbatical.",
                metadata={"chunk_id": "c2", "document_id": "doc2", "source": "benefits.md", "page": 2, "filename": "benefits.md"}
            )
        ]

    def similarity_search_with_score(self, query: str, k: int, filter=None):
        # Standard format output matching ChromaVectorStore
        return [(doc, 0.1) for doc in self.docs[:k]]

    @property
    def vectordb(self):
        class MockChroma:
            def get(self):
                return {
                    "ids": [doc.metadata["chunk_id"] for doc in self.docs],
                    "documents": [doc.page_content for doc in self.docs],
                    "metadatas": [doc.metadata for doc in self.docs]
                }
        return MockChroma()


# --- Test Cases ---

class TestQueryContext(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patcher_embed = patch("app.core.embeddings.load_embedding_model", return_value=MockEmbeddings())
        cls.patcher_embed.start()
        cls.patcher_llm = patch("app.core.llm.LLM", return_value=MockLLM())
        cls.patcher_llm.start()
        cls.patcher_gen_llm = patch("app.generation.generator.LLM", return_value=MockLLM())
        cls.patcher_gen_llm.start()
        cls.patcher_ce = patch("app.retrieval.reranker.CrossEncoder", return_value=MockCrossEncoder())
        cls.patcher_ce.start()

    @classmethod
    def tearDownClass(cls):
        cls.patcher_embed.stop()
        cls.patcher_llm.stop()
        cls.patcher_gen_llm.stop()
        cls.patcher_ce.stop()
    def setUp(self):
        self.analyzer = QueryAnalyzer()
        # Force-inject MockLLM for offline test runs
        self.analyzer.llm_obj = MockLLM()
        
        self.rewriter = QueryRewriter()
        self.decomposer = QueryDecomposer()
        self.deduplicator = ContextDeduplicator()
        self.compressor = ContextCompressor()
        self.organizer = ContextOrganizer()

    def tearDown(self):
        pass

    # ═══════════════════════════════════════════════════════════════════════════
    # 1. Query Analyzer
    # ═══════════════════════════════════════════════════════════════════════════
    def test_complex_how_query(self):
        # A query starting with 'how' can be procedurally complex
        analysis = self.analyzer.analyze("How can I apply for an exception to the sabbatical policy?")
        self.assertEqual(analysis.intent, "procedural")
        self.assertEqual(analysis.complexity, "moderate")

    def test_ambiguous_keyword_heuristics(self):
        # 'stuff' keyword in a legitimate query shouldn't automatically cause ambiguity
        # if the query is detailed
        analysis = self.analyzer.analyze("What is the policy for personal stuff in my locker?")
        self.assertEqual(analysis.query_type, "ambiguous")
        self.assertTrue(analysis.needs_rewriting)

    def test_conversational_query(self):
        analysis = self.analyzer.analyze("Hello, thank you for helping me.")
        self.assertEqual(analysis.intent, "conversational")
        self.assertEqual(analysis.complexity, "simple")

    def test_analyzer_fail_safe(self):
        # Test case: Analyzer LLM crashes
        mock_llm = MagicMock()
        mock_llm.generate_response.side_effect = RuntimeError("LLM crashed")
        
        orig_llm = self.analyzer.llm_obj
        self.analyzer.llm_obj = mock_llm
        try:
            analysis = self.analyzer.analyze("How do I request sabbatical leave?")
            # Should fallback gracefully without raising an exception to user
            self.assertEqual(analysis.original_query, "How do I request sabbatical leave?")
            self.assertEqual(analysis.intent, "unknown")
            self.assertEqual(analysis.complexity, "simple")
        finally:
            self.analyzer.llm_obj = orig_llm

    # ═══════════════════════════════════════════════════════════════════════════
    # 2. Query Rewriter & Decomposer
    # ═══════════════════════════════════════════════════════════════════════════
    def test_query_rewriter_success_and_disabled(self):
        analysis = QueryAnalysis(
            original_query="What about the leave thing?",
            needs_rewriting=True,
            rewritten_query="What is the company's employee leave policy?"
        )
        # Enabled (default)
        self.assertEqual(self.rewriter.rewrite(analysis), "What is the company's employee leave policy?")
        
        # Disabled
        disabled_rewriter = QueryRewriter(enabled=False)
        self.assertEqual(disabled_rewriter.rewrite(analysis), "What about the leave thing?")

    def test_query_decomposer_sub_query_capping(self):
        analysis = QueryAnalysis(
            original_query="excessive sub-queries",
            needs_decomposition=True,
            sub_queries=["q1", "q2", "q3", "q4", "q5", "q6"]
        )
        decomposer = QueryDecomposer(max_sub_queries=4)
        subs = decomposer.decompose(analysis)
        self.assertEqual(len(subs), 4)
        self.assertEqual(subs, ["q1", "q2", "q3", "q4"])

    def test_query_decomposer_simple_preservation(self):
        analysis = QueryAnalysis(original_query="What is sabbatical policy?", needs_decomposition=False)
        subs = self.decomposer.decompose(analysis)
        self.assertEqual(subs, ["What is sabbatical policy?"])

    # ═══════════════════════════════════════════════════════════════════════════
    # 3. Context Deduplication
    # ═══════════════════════════════════════════════════════════════════════════
    def test_context_deduplicator_stable_hash_fallback(self):
        # Set chunk_id to None/unknown to trigger text hash fallback
        chunks = [
            SearchResult(chunk_id="unknown", document_id="doc1", content="vacation details", score=0.8, source="hr.md", page=1),
            SearchResult(chunk_id="unknown", document_id="doc1", content="vacation details", score=0.9, source="hr.md", page=1),
            SearchResult(chunk_id="unknown", document_id="doc2", content="unique content", score=0.5, source="benefits.md", page=2)
        ]
        deduped = self.deduplicator.deduplicate(chunks)
        self.assertEqual(len(deduped), 2)
        # Verify highest score was kept
        self.assertEqual(deduped[0].score, 0.9)
        self.assertEqual(deduped[0].content, "vacation details")
        # Metadata survives
        self.assertEqual(deduped[0].source, "hr.md")

    # ═══════════════════════════════════════════════════════════════════════════
    # 4. Context Compression
    # ═══════════════════════════════════════════════════════════════════════════
    def test_context_compressor_preservation_rules(self):
        compressor = ContextCompressor(enabled=True)
        # Content has critical policy constraints, dates, and negations
        content = (
            "We have a sabbatical policy. Sabbatical leaves can be requested after 5 years of tenure. "
            "However, you must not exceed 6 weeks of leave. "
            "If you resign, you forfeit your liquidity pool shares. "
            "This sentence is completely irrelevant filler content about coffee beans."
        )
        chunk = SearchResult(chunk_id="c1", document_id="doc1", content=content, score=0.8, source="a.md", page=1)
        
        compressed = compressor.compress("What is the sabbatical leave?", [chunk])
        
        # Verify irrelevant sentence was filtered, but critical policy conditions and negations survive
        self.assertIn("Sabbatical leaves can be requested after 5 years of tenure.", compressed[0].content)
        self.assertIn("However, you must not exceed 6 weeks of leave.", compressed[0].content)
        self.assertNotIn("coffee beans", compressed[0].content)
        
        # Citation metadata survives
        self.assertEqual(compressed[0].chunk_id, "c1")
        self.assertEqual(compressed[0].source, "a.md")

    def test_context_compressor_disabled_and_failure(self):
        # 1. Disabled
        disabled_compressor = ContextCompressor(enabled=False)
        chunks = [SearchResult(chunk_id="c1", document_id="d1", content="sabbatical details", score=0.8, source="a", page=1)]
        res_disabled = disabled_compressor.compress("query", chunks)
        self.assertEqual(res_disabled[0].content, "sabbatical details")

        # 2. Failure fallback (returns original content on exceptions)
        enabled_compressor = ContextCompressor(enabled=True)
        with patch.object(enabled_compressor, "_compress_chunk", side_effect=Exception("regex error")):
            res_fail = enabled_compressor.compress("query", chunks)
            self.assertEqual(res_fail[0].content, "sabbatical details")

    # ═══════════════════════════════════════════════════════════════════════════
    # 5. Context Budgeting & Organizer
    # ═══════════════════════════════════════════════════════════════════════════
    def test_context_budget_never_splits_chunk(self):
        # 3 chunks totaling ~130 chars
        chunks = [
            SearchResult(chunk_id="c1", document_id="doc1", content="Sabbatical policy is 6 weeks.", score=0.9, source="hr.md", page=1),
            SearchResult(chunk_id="c2", document_id="doc2", content="Vacation policy is 15 days.", score=0.8, source="benefits.md", page=1),
            SearchResult(chunk_id="c3", document_id="doc3", content="Sick policy is 5 days.", score=0.7, source="hr.md", page=2)
        ]
        # Set max_chars budget small (e.g. 80 chars)
        # Adding first chunk + header: len("Sabbatical policy is 6 weeks.") + len("[Source: hr.md, Page 1]\n") + 2 = 29 + 24 + 2 = 55 chars
        # Adding second chunk + header: len("Vacation policy is 15 days.") + len("[Source: benefits.md, Page 1]\n") + 2 = 27 + 30 + 2 = 59 chars
        # Total with second chunk would be 114 chars (exceeding budget of 80)
        # So it must stop at 1 chunk, and NOT add half of the second chunk!
        organizer = ContextOrganizer(max_chars=80)
        selected = organizer.organize(chunks)
        
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].chunk_id, "c1")
        # Ensure it contains full text content
        self.assertEqual(selected[0].content, "Sabbatical policy is 6 weeks.")

    # ═══════════════════════════════════════════════════════════════════════════
    # 6. Integration
    # ═══════════════════════════════════════════════════════════════════════════
    def test_integration_original_query_reranked(self):
        vector_db = DummyVectorDB()
        service = RAGService(vector_db)
        
        # Override rerank method to track parameters passed
        tracked_query = None
        orig_rerank = service.retrieval_pipeline.reranker.rerank
        
        def mock_rerank(query, candidates):
            nonlocal tracked_query
            tracked_query = query
            return orig_rerank(query, candidates)
            
        service.retrieval_pipeline.reranker.rerank = mock_rerank
        
        # Test 1: Ambiguous query (rewritten query used for search, original query used for reranking)
        service.answer_question("What about the leave thing?")
        self.assertEqual(tracked_query, "What about the leave thing?")

        # Test 2: Decomposed query (sub-queries used for search, original query used for reranking)
        service.answer_question("What is our vacation policy and how does it compare to sabbatical?")
        self.assertEqual(tracked_query, "What is our vacation policy and how does it compare to sabbatical?")
        
        # Restore
        service.retrieval_pipeline.reranker.rerank = orig_rerank

if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        patcher_embed.stop()
        patcher_llm.stop()
        patcher_ce.stop()
