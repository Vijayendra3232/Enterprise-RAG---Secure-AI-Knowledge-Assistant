"""
test_grounding.py — Unit and integration tests for Step 5 Grounded Generation and Factual Verification.

Covers:
  - CitationBuilder: citation creation, metadata preservation, sequential IDs.
  - CitationValidator: fabricated ID checks, mismatch filtering.
  - ClaimExtractor: JSON parsing and sentence-level fallback.
  - GroundingVerifier:
    - Cosine similarity overrides.
    - Contradictory numbers / durations / frequencies ("8-week sabbatical every two years" vs "6-week sabbatical every three years").
    - Contradictory negations ("may take" vs "may not take").
    - Partial claim support (modal mismatch "preferred" vs "mandatory").
    - Unsupported claims.
  - AnswerPolicy:
    - Unsupported claim removal / sentence filtering.
    - Out-of-scope question refusal ("private jets").
"""

import os
import sys
import re
import unittest
from unittest.mock import MagicMock, patch

# Add backend directory to sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

# --- Offline Mocks ---

class MockEmbeddings:
    def embed_documents(self, texts):
        return [[0.1] * 384 for _ in texts]
    def embed_query(self, text):
        # We can mock vectors to return identical weights, meaning cosine similarity will be near 1.0.
        # This allows us to verify that our verifier's numeric and negation overrides work
        # and override similarity score even when cosine similarity is extremely high!
        return [0.1] * 384

class MockLLM:
    def __init__(self, *args, **kwargs):
        pass
    def generate_response(self, prompt):
        prompt_lower = prompt.lower()
        if "semantically equivalent search queries" in prompt_lower or "search queries" in prompt_lower:
            return '["query 1", "query 2"]'
        if "50 crore" in prompt_lower and "revenue" in prompt_lower:
            return """{
                "answer": "Total secret revenue for Q3 reached 50 crore INR. [C1]",
                "claims": [{"text": "Total secret revenue for Q3 reached 50 crore INR.", "evidence_ids": ["C1"]}]
            }"""
        elif "80%" in prompt_lower and ("test coverage" in prompt_lower or "coverage" in prompt_lower) and "revenue" not in prompt_lower and "company b" not in prompt_lower:
            return """{
                "answer": "All backend microservices must maintain 80% automated test coverage. [C1]",
                "claims": [{"text": "All backend microservices must maintain 80% automated test coverage.", "evidence_ids": ["C1"]}]
            }"""
        elif "private jet" in prompt_lower:
            return '{"answer": "I don\'t know.", "claims": [], "refusal": true}'
        elif "sabbatical policy" in prompt_lower:
            return """{
                "answer": "Employees may take a 6-week paid sabbatical every three years. [C1]",
                "claims": [
                    {
                        "text": "Employees may take a 6-week paid sabbatical every three years.",
                        "evidence_ids": ["C1"]
                    }
                ]
            }"""
        elif "unsupported claim test" in prompt_lower:
            # Parse the context block dynamically to determine C1/C2 order
            sabbatical_tag = "C1"
            vacation_tag = "C2"
            matches = re.findall(r"\[(c\d+)\]\s*\n(.*?)(?=\n\[c\d+\]|$)", prompt_lower, re.DOTALL)
            for tag, content in matches:
                if "sabbatical" in content:
                    sabbatical_tag = tag.upper()
                if "vacation" in content:
                    vacation_tag = tag.upper()
                    
            # Contains an unsupported claim (8-week paid sabbatical) and a supported one
            return f"""{{
                "answer": "Employees may take an 8-week paid sabbatical every two years [{sabbatical_tag}]. Also, they get 20 days of vacation [{vacation_tag}].",
                "claims": [
                    {{
                        "text": "Employees may take an 8-week paid sabbatical every two years.",
                        "evidence_ids": ["{sabbatical_tag}"]
                    }},
                    {{
                        "text": "They get 20 days of vacation.",
                        "evidence_ids": ["{vacation_tag}"]
                    }}
                ]
            }}"""
        return '{"answer": "I don\'t know.", "claims": [], "refusal": true}'

class MockCrossEncoder:
    def __init__(self, *args, **kwargs):
        pass
    def predict(self, pairs):
        return [0.95 for _ in pairs]

# Import modules
from app import config
from app.retrieval.models import SearchResult
from app.grounding.models import Claim
from app.grounding.citation import CitationBuilder, CitationValidator
from app.grounding.claims import ClaimExtractor
from app.grounding.verifier import GroundingVerifier, LexicalVerifier, SemanticVerifier
from app.services.rag_service import RAGService
from app.retrieval.vector import VectorStoreInterface
from langchain_core.documents import Document

class DummyVectorDB(VectorStoreInterface):
    def __init__(self):
        self.docs = [
            Document(
                page_content="Every three years employees may take a 6-week paid sabbatical. Give your team a heads-up.",
                metadata={"chunk_id": "c1", "document_id": "doc1", "source": "benefits.md", "page": 1, "filename": "benefits.md"}
            ),
            Document(
                page_content="37signals offers 20 days of vacation and personal days.",
                metadata={"chunk_id": "c2", "document_id": "doc2", "source": "vacation.md", "page": 1, "filename": "vacation.md"}
            )
        ]

    def similarity_search_with_score(self, query: str, k: int, filter=None):
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

class TestGrounding(unittest.TestCase):
    def setUp(self):
        self.patch_embed = patch("app.core.embeddings.load_embedding_model", return_value=MockEmbeddings())
        self.patch_llm = patch("app.core.llm.LLM", return_value=MockLLM())
        self.patch_gen_llm = patch("app.generation.generator.LLM", return_value=MockLLM())
        import app.retrieval.reranker
        self.patch_ce = patch("app.retrieval.reranker.CrossEncoder", return_value=MockCrossEncoder())
        self.patch_embed.start()
        self.patch_llm.start()
        self.patch_gen_llm.start()
        self.patch_ce.start()

        self.chunks = [
            SearchResult(chunk_id="chunk_1", document_id="doc_1", content="Sabbatical leave is 6 weeks.", score=0.9, source="hr.md", page=1),
            SearchResult(chunk_id="chunk_2", document_id="doc_2", content="Vacation is 20 days.", score=0.8, source="benefits.md", page=1)
        ]
        self.builder = CitationBuilder()
        self.citations, self.mapping = self.builder.build_citations(self.chunks)

    def tearDown(self):
        self.patch_embed.stop()
        self.patch_llm.stop()
        self.patch_gen_llm.stop()
        self.patch_ce.stop()
        self.builder = CitationBuilder()
        self.citations, self.mapping = self.builder.build_citations(self.chunks)

    # ═══════════════════════════════════════════════════════════════════════════
    # 1. Citation Builder and Validator
    # ═══════════════════════════════════════════════════════════════════════════
    def test_citation_builder(self):
        self.assertEqual(len(self.citations), 2)
        self.assertEqual(self.citations[0].citation_id, "C1")
        self.assertEqual(self.citations[0].document_id, "doc_1")
        self.assertEqual(self.citations[0].filename, "hr.md")
        self.assertEqual(self.citations[0].page, 1)

    def test_citation_validator_fabricated_tags(self):
        validator = CitationValidator(self.mapping)
        # C999 is fabricated. It must be stripped.
        text = "Employees get 6 weeks of leave [C1] and free lunches [C999]."
        cleaned, evidence = validator.validate_citations(text, ["C1", "C999"])
        
        self.assertIn("[C1]", cleaned)
        self.assertNotIn("[C999]", cleaned)
        # Mappings of claim evidence should also filter out C999
        self.assertEqual(evidence, ["C1"])

    # ═══════════════════════════════════════════════════════════════════════════
    # 2. Claim Extraction
    # ═══════════════════════════════════════════════════════════════════════════
    def test_claim_extractor(self):
        extractor = ClaimExtractor()
        # Test JSON extraction
        structured = [
            {"text": "Factual assertion 1. [C1]", "evidence_ids": ["C1"]},
            {"text": "Factual assertion 2.", "evidence_ids": ["C2"]}
        ]
        claims = extractor.extract_claims("raw text", structured)
        self.assertEqual(len(claims), 2)
        self.assertEqual(claims[0].text, "Factual assertion 1.")
        self.assertEqual(claims[0].evidence_ids, ["C1"])

        # Test sentence fallback extraction
        text_fallback = "Sabbatical leave is 6 weeks [C1]. Vacation is 20 days [C2]."
        claims_fallback = extractor.extract_claims(text_fallback, None)
        self.assertEqual(len(claims_fallback), 2)
        self.assertEqual(claims_fallback[0].text, "Sabbatical leave is 6 weeks.")
        self.assertEqual(claims_fallback[0].evidence_ids, ["C1"])

    # ═══════════════════════════════════════════════════════════════════════════
    # 3. Grounding Verifier Checks (Embedding overrides)
    # ═══════════════════════════════════════════════════════════════════════════
    def test_grounding_verifier_supported(self):
        verifier = GroundingVerifier()
        status = verifier.verify_claim(
            "Employees may take a 6-week paid sabbatical.",
            "Every three years employees may take a 6-week paid sabbatical."
        )
        self.assertEqual(status, "SUPPORTED")

    def test_semantic_similarity_but_contradictory_number(self):
        # CRITICAL TEST CASE: Mismatched numbers must override similarity
        verifier = GroundingVerifier()
        # Even with high similarity (mock returns identical embeddings), this MUST NOT be supported
        status = verifier.verify_claim(
            "Employees may take an 8-week paid sabbatical every two years.",
            "Every three years employees may take a 6-week paid sabbatical."
        )
        self.assertEqual(status, "UNSUPPORTED")

    def test_semantic_similarity_but_contradictory_negation(self):
        # Mismatched negation must override similarity
        verifier = GroundingVerifier()
        status = verifier.verify_claim(
            "Employees may not take sabbatical leave every three years.",
            "Every three years employees may take sabbatical leave."
        )
        self.assertEqual(status, "UNSUPPORTED")

    def test_partial_claim_support_modal_mismatch(self):
        # mandatory vs preferred
        verifier = GroundingVerifier()
        status = verifier.verify_claim(
            "Three months advance notice is mandatory.",
            "Three months advance notice is preferred."
        )
        self.assertEqual(status, "PARTIALLY_SUPPORTED")

    # ═══════════════════════════════════════════════════════════════════════════
    # 4. Answer Policy and Refusal
    # ═══════════════════════════════════════════════════════════════════════════
    def test_answer_policy_sentence_filtering(self):
        vector_db = DummyVectorDB()
        service = RAGService(vector_db)
        
        # Sabbatical claim is unsupported ("8-week sabbatical every two years")
        # Vacation claim is supported ("20 days of vacation")
        # The service must filter out the unsupported sabbatical sentence
        response = service.answer_question_grounded("unsupported claim test")
        
        self.assertEqual(response.grounding.status, "PARTIALLY_SUPPORTED")
        self.assertIn("Also, they get 20 days of vacation", response.response)
        # Ensure the 8-week claim sentence is removed
        self.assertNotIn("8-week paid sabbatical", response.response)

    def test_out_of_scope_refusal(self):
        vector_db = DummyVectorDB()
        service = RAGService(vector_db)
        
        # Out-of-scope question ("private jet") should refuse to answer
        response = service.answer_question_grounded("What is the company's private jet purchasing policy?")
        self.assertEqual(response.grounding.status, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(response.response, "I couldn't find enough information in the available documents to answer that reliably.")
        self.assertEqual(len(response.citations), 0)

if __name__ == "__main__":
    unittest.main()
