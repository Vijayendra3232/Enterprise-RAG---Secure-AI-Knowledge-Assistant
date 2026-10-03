import os
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

# Base directory of the repository
_app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_parent_dir = os.path.dirname(_app_dir)
BASE_DIR = _app_dir if _parent_dir == os.path.abspath(os.path.sep) else _parent_dir

# --- LLM / Embedding ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")
GROQ_MODEL_ID = os.getenv("GROQ_MODEL_ID", "openai/gpt-oss-120b")

def _resolve_path(path: str) -> str:
    """Resolve paths relative to project root if they are not absolute."""
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(BASE_DIR, path))

# --- Vector Store ---
VECTOR_DB_DIR = _resolve_path(os.getenv("VECTOR_DB_DIR", "dir3"))
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "collection")
DATA_DIR = _resolve_path(os.getenv("DATA_DIR", "data/handbook-master"))

# --- Cache ---
CACHE_TIMEOUT = int(os.getenv("CACHE_TIMEOUT", "1800"))
MAX_CACHE_SIZE = int(os.getenv("MAX_CACHE_SIZE", "500"))

# --- Chunking ---
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "50"))

# --- Ingestion / Upload ---
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
UPLOAD_DIR = _resolve_path(os.getenv("UPLOAD_DIR", "data/uploads"))

ALLOWED_EXTENSIONS: set[str] = {"pdf", "docx", "txt", "md", "csv", "xlsx", "json"}

# --- Retrieval Configuration ---
VECTOR_TOP_K = int(os.getenv("VECTOR_TOP_K", "10"))
BM25_TOP_K = int(os.getenv("BM25_TOP_K", "10"))
HYBRID_TOP_K = int(os.getenv("HYBRID_TOP_K", "10"))
RERANK_TOP_K = int(os.getenv("RERANK_TOP_K", "5"))
RRF_K = int(os.getenv("RRF_K", "60"))
VECTOR_WEIGHT = float(os.getenv("VECTOR_WEIGHT", "0.5"))
BM25_WEIGHT = float(os.getenv("BM25_WEIGHT", "0.5"))
ENABLE_RERANKER = os.getenv("ENABLE_RERANKER", "true").lower() == "true"
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "ms-marco-MiniLM-L-6-v2")
RETRIEVAL_STRATEGY = os.getenv("RETRIEVAL_STRATEGY", "fusion")

# --- Query Intelligence & Context Optimization ---
ENABLE_QUERY_REWRITING = os.getenv("ENABLE_QUERY_REWRITING", "true").lower() == "true"
ENABLE_QUERY_DECOMPOSITION = os.getenv("ENABLE_QUERY_DECOMPOSITION", "true").lower() == "true"
MAX_SUB_QUERIES = int(os.getenv("MAX_SUB_QUERIES", "4"))
ENABLE_CONTEXT_COMPRESSION = os.getenv("ENABLE_CONTEXT_COMPRESSION", "false").lower() == "true"
MAX_CONTEXT_CHUNKS = int(os.getenv("MAX_CONTEXT_CHUNKS", "8"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "4000"))
# --- Grounded Generation & Citation Verification ---
ENABLE_GROUNDING_VERIFICATION = os.getenv("ENABLE_GROUNDING_VERIFICATION", "true").lower() == "true"
GROUNDING_METHOD = os.getenv("GROUNDING_METHOD", "semantic")
MIN_GROUNDING_SCORE = float(os.getenv("MIN_GROUNDING_SCORE", "0.70"))
REFUSE_ON_UNSUPPORTED_CLAIMS = os.getenv("REFUSE_ON_UNSUPPORTED_CLAIMS", "true").lower() == "true"

# --- Enterprise Authentication & Authorization ---
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-secret-key-change-in-production-1234567890")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
_raw_algs = [
    a.strip()
    for a in os.getenv("JWT_ALLOWED_ALGORITHMS", JWT_ALGORITHM).split(",")
    if a.strip() and a.strip().lower() != "none" and a.strip() != "*"
]
JWT_ALLOWED_ALGORITHMS = _raw_algs if _raw_algs else ["HS256"]
JWT_ISSUER = os.getenv("JWT_ISSUER", "enterprise-rag-auth")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "enterprise-rag-api")
JWT_LEEWAY_SECONDS = int(os.getenv("JWT_LEEWAY_SECONDS", "0"))
AUTH_TRUSTED_ISSUERS = [i.strip() for i in os.getenv("AUTH_TRUSTED_ISSUERS", JWT_ISSUER).split(",") if i.strip()]
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
APP_ENV = os.getenv("APP_ENV", ENVIRONMENT).lower()
DEFAULT_TENANT_ID = os.getenv("DEFAULT_TENANT_ID", "default_tenant")
AUTO_SEED_DATA = os.getenv("AUTO_SEED_DATA", "false").lower() == "true"
USER_REPOSITORY_TYPE = os.getenv("USER_REPOSITORY_TYPE", "sql").lower()



# --- Production Persistent Storage & Database Configuration ---
_default_sqlite_path = os.getenv("SQLITE_DB_PATH")
if not _default_sqlite_path:
    if os.path.exists("/tmp/scratch"):
        _default_sqlite_path = "/tmp/scratch/metadata.db"
    elif os.path.exists("/tmp"):
        _default_sqlite_path = "/tmp/metadata.db"
    else:
        _default_sqlite_path = "data/metadata.db"

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{_resolve_path(_default_sqlite_path)}"
)
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "20"))
DB_POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "30"))
DB_POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800"))
DB_POOL_PRE_PING = os.getenv("DB_POOL_PRE_PING", "true").lower() == "true"

VECTOR_STORE_TYPE = os.getenv("VECTOR_STORE_TYPE", "chroma")
KEYWORD_SEARCH_TYPE = os.getenv("KEYWORD_SEARCH_TYPE", "bm25")

# --- Production Cloud Storage (Amazon S3 + SSE-KMS CMK) ---
DOCUMENT_STORAGE_TYPE = os.getenv("DOCUMENT_STORAGE_TYPE", "local").lower()
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_REGION = os.getenv("S3_REGION", "us-east-1")
S3_KMS_KEY_ID = os.getenv("S3_KMS_KEY_ID", "")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", None)
S3_CONNECT_TIMEOUT = int(os.getenv("S3_CONNECT_TIMEOUT", "5"))
S3_READ_TIMEOUT = int(os.getenv("S3_READ_TIMEOUT", "10"))

# --- Connectors & Secret Storage (AWS Secrets Manager) ---
SECRET_KEY = os.getenv("SECRET_KEY", JWT_SECRET_KEY)
SECRET_PROVIDER_TYPE = os.getenv("SECRET_PROVIDER_TYPE", "local").lower()
AWS_SECRET_PREFIX = os.getenv("AWS_SECRET_PREFIX", "enterprise-rag/")
AWS_SECRETS_REGION = os.getenv("AWS_SECRETS_REGION", "us-east-1")
AWS_SECRETS_ENDPOINT_URL = os.getenv("AWS_SECRETS_ENDPOINT_URL", None)
SECRET_CACHE_TTL_SECONDS = int(os.getenv("SECRET_CACHE_TTL_SECONDS", "300"))
SECRET_CACHE_MAX_ENTRIES = int(os.getenv("SECRET_CACHE_MAX_ENTRIES", "1000"))
MAX_SYNC_DOCUMENTS = int(os.getenv("MAX_SYNC_DOCUMENTS", "10000"))

# --- Production Search Infrastructure ---
SEARCH_STORE_TYPE = os.getenv("SEARCH_STORE_TYPE", "chroma_dev").lower()
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
OPENSEARCH_INDEX_PREFIX = os.getenv("OPENSEARCH_INDEX_PREFIX", "enterprise_rag_chunks")
OPENSEARCH_TIMEOUT_SECONDS = int(os.getenv("OPENSEARCH_TIMEOUT_SECONDS", "10"))
REBUILD_ROLLBACK_RETENTION_HOURS = int(os.getenv("REBUILD_ROLLBACK_RETENTION_HOURS", "24"))
EMBEDDING_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "384"))

# --- Asynchronous Task Execution & Worker Architecture ---
WORKER_EMBEDDED = os.getenv("WORKER_EMBEDDED", "false").lower() == "true"  # DEV/TEST ONLY
WORKER_CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY", "2"))
WORKER_POLL_INTERVAL_SECONDS = float(os.getenv("WORKER_POLL_INTERVAL_SECONDS", "1.0"))
TASK_CLAIM_TIMEOUT_SECONDS = int(os.getenv("TASK_CLAIM_TIMEOUT_SECONDS", "300"))
TASK_MAX_ATTEMPTS = int(os.getenv("TASK_MAX_ATTEMPTS", "3"))
TASK_RETRY_BASE_DELAY_SECONDS = int(os.getenv("TASK_RETRY_BASE_DELAY_SECONDS", "2"))
TASK_RETRY_MAX_DELAY_SECONDS = int(os.getenv("TASK_RETRY_MAX_DELAY_SECONDS", "60"))
TASK_STALE_THRESHOLD_SECONDS = int(os.getenv("TASK_STALE_THRESHOLD_SECONDS", "300"))
MAX_TASK_PAYLOAD_SIZE = int(os.getenv("MAX_TASK_PAYLOAD_SIZE", "65536"))

# --- Observability, Monitoring & Telemetry Configuration ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "json").lower()
ENABLE_TRACING = os.getenv("ENABLE_TRACING", "true").lower() == "true"
TRACE_SAMPLE_RATE = float(os.getenv("TRACE_SAMPLE_RATE", "1.0"))
ENABLE_METRICS = os.getenv("ENABLE_METRICS", "true").lower() == "true"
MONITORING_API_KEY = os.getenv("MONITORING_API_KEY", "")
TELEMETRY_HMAC_KEY = os.getenv("TELEMETRY_HMAC_KEY", "")
SLOW_REQUEST_THRESHOLD_MS = int(os.getenv("SLOW_REQUEST_THRESHOLD_MS", "2000"))
SLOW_DB_THRESHOLD_MS = int(os.getenv("SLOW_DB_THRESHOLD_MS", "200"))
SLOW_SEARCH_THRESHOLD_MS = int(os.getenv("SLOW_SEARCH_THRESHOLD_MS", "500"))
SLOW_LLM_THRESHOLD_MS = int(os.getenv("SLOW_LLM_THRESHOLD_MS", "3000"))

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Fail securely in production if secret key is default or missing
if ENVIRONMENT == "production" or APP_ENV == "production":
    if not JWT_SECRET_KEY or JWT_SECRET_KEY == "dev-secret-key-change-in-production-1234567890":
        raise RuntimeError("CRITICAL SECURITY ERROR: Insecure or default JWT_SECRET_KEY detected in production environment.")
    if DOCUMENT_STORAGE_TYPE == "s3" and not S3_BUCKET:
        raise RuntimeError("CRITICAL CONFIGURATION ERROR: S3_BUCKET must be specified when DOCUMENT_STORAGE_TYPE='s3' in production.")

