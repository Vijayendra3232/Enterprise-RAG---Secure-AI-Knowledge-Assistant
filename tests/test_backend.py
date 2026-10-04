import os
import sys
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

# 1. Update sys.path first so Python can find the 'app' module
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

# 2. Mock embeddings class to run tests entirely offline
class MockEmbeddings:
    def embed_documents(self, texts):
        return [[0.1] * 384 for _ in texts]
    def embed_query(self, text):
        return [0.1] * 384

# 3. Conditional LLM mock class if GROQ_API_KEY is not set
class MockLLM:
    def __init__(self, *args, **kwargs):
        self.llm_params = args[0] if args else {}
        self.model_id = args[1] if len(args) > 1 else "mock-model"
    def generate_response(self, prompt):
        if "semantically equivalent search queries" in prompt:
            return '["query 1", "query 2"]'
        return """{
            "answer": "Mock LLM Response with sufficient details.",
            "claims": [{"text": "Mock LLM Response with sufficient details.", "evidence_ids": ["C1"]}]
        }"""

# Now safe to import app components
from app.main import app
from app import config
from app.ingestion.pipeline import IngestionPipeline

class TestBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patcher = patch('app.core.embeddings.load_embedding_model', return_value=MockEmbeddings())
        cls.patcher.start()
        cls.llm_patcher = None
        if not os.getenv("GROQ_API_KEY"):
            cls.llm_patcher = patch('app.core.llm.LLM', return_value=MockLLM())
            cls.llm_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()
        if cls.llm_patcher:
            cls.llm_patcher.stop()
    def test_config(self):
        """Verify configuration is loaded correctly."""
        print("\nTesting Configuration...")
        self.assertIsNotNone(config.VECTOR_DB_DIR)
        self.assertIsNotNone(config.COLLECTION_NAME)
        self.assertIsNotNone(config.DATA_DIR)
        print(f"Config is valid. DB directory: {config.VECTOR_DB_DIR}")

    def test_health_endpoint(self):
        """Test health check route."""
        print("\nTesting /health endpoint...")
        with TestClient(app) as client:
            response = client.get("/health")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "healthy"})
        print("Health check passed.")

    def test_chat_integration(self):
        """
        Test full end-to-end flow:
        - Run IngestionPipeline on the handbook files.
        - Connect to the vector database.
        - Query the Chat Assistant endpoint.
        """
        # Verify GROQ_API_KEY is present
        if not config.GROQ_API_KEY:
            print("\nGROQ_API_KEY not found in environment. Skipping LLM/Chat integrations test.")
            return

        print("\nRunning Ingestion Pipeline on handbook-master files...")
        # 1. Run pipeline
        pipeline = IngestionPipeline()
        vectordb = pipeline.run(
            data_dir=config.DATA_DIR,
            persist_dir=config.VECTOR_DB_DIR,
            collection_name=config.COLLECTION_NAME
        )
        
        self.assertIsNotNone(vectordb)
        
        print("Testing Chat-Assistant query...")
        from app.auth.jwt import create_access_token
        token = create_access_token({
            "sub": "admin_a",
            "tenant_id": "company_a",
            "email": "admin@companya.com",
            "name": "Admin",
            "role": "ADMIN"
        })
        with TestClient(app) as client:
            # Ask a question from the handbook content
            question = "What is the policy for Paid Sick Time?"
            response = client.get(
                f"/Chat-Assistant/?question={question}",
                headers={"Authorization": f"Bearer {token}"}
            )
            
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("response", data)
            self.assertIn("docs", data)
            self.assertGreater(len(data["docs"]), 0)
            
            # Verify first document structure
            doc = data["docs"][0]
            self.assertIn("chunk", doc)
            self.assertIn("page", doc)
            self.assertIn("file", doc)
            
            print("\n=== Chat Integration Test Success ===")
            print(f"Question: {question}")
            print(f"Response: {data['response'][:150]}...")

if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        patcher.stop()
        if llm_patcher:
            llm_patcher.stop()
