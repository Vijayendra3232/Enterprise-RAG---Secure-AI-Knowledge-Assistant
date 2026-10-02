import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from app.api import chat, health, auth
from app.api import documents, connectors, tasks
from app.retrieval.vector import get_vector_store
from app.services.rag_service import RAGService
from app import config

from app.storage.database import init_db

# Load environment variables
load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize metadata database tables
    try:
        init_db()
    except Exception as e:
        print(f"[Main] Database initialization warning: {e}")

    # Initialize provider-agnostic search store (OpenSearch or Dev Adapter)
    try:
        from app.storage.search.factory import get_search_store
        search_store = get_search_store()
        app.state.search_store = search_store
    except Exception as e:
        print(f"[Main] Search store initialization warning: {e}")
        search_store = None

    # Fallback to vector database if needed
    vectordb = None
    if search_store is None:
        vectordb = get_vector_store(
            persist_dir=config.VECTOR_DB_DIR,
            collection_name=config.COLLECTION_NAME,
            embedding_model_name=config.EMBEDDING_MODEL_NAME,
        )

    # Initialize global RAG Service and attach to app state
    app.state.rag_service = RAGService(
        vector_db=vectordb,
        search_store=search_store,
        llm_model_id=config.GROQ_MODEL_ID,
    )

    # Embedded worker pool for local testing and development only
    embedded_pool = None
    if getattr(config, "WORKER_EMBEDDED", False):
        print("[Lifespan] Starting Embedded Worker Pool [DEV/TEST ONLY]")
        from app.tasks.worker import WorkerPool
        embedded_pool = WorkerPool(
            concurrency=config.WORKER_CONCURRENCY,
            rag_service=app.state.rag_service,
        )
        embedded_pool.start()

    yield

    if embedded_pool:
        print("[Lifespan] Stopping Embedded Worker Pool...")
        embedded_pool.stop()

from app.observability.middleware import ObservabilityMiddleware, metrics_router

app = FastAPI(
    title="Enterprise RAG API",
    description="Production-grade Retrieval-Augmented Generation platform with enterprise authentication, authorization, persistent storage, production OpenSearch infrastructure, cloud connectors, and distributed asynchronous task execution.",
    version="0.10.0",
    lifespan=lifespan,
)

# Attach Observability Middleware first to capture request context and metrics
app.add_middleware(ObservabilityMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(health.router)
app.include_router(documents.router)
app.include_router(connectors.router)
app.include_router(tasks.router)
app.include_router(metrics_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8500, log_level="info")
