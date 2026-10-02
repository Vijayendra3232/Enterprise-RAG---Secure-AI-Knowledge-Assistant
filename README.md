# Enterprise RAG Platform

A modular, production-grade implementation of an Enterprise Retrieval-Augmented Generation (RAG) platform.

## Architecture

The project is structured modularly to separate API endpoints, ingestion pipelines, search retrieval logic, LLM interaction, and configuration.

```
enterprise-rag/
├── backend/
│   ├── app/
│   │   ├── main.py                     # FastAPI application entrypoint
│   │   ├── config.py                   # Configuration and path resolver
│   │   ├── api/                        # HTTP routers (chat, health check)
│   │   ├── core/                       # LLM and embeddings wrappers
│   │   ├── ingestion/                  # Document loader, chunker, and database writers
│   │   ├── retrieval/                  # Vector db wrappers & RAG Fusion search
│   │   └── services/                   # Orchestrators (RAGService)
│   ├── requirements.txt                # Python package list
│   └── Dockerfile                      # Production Docker builder
│
├── data/                               # Directory containing source datasets
│   └── handbook-master/                # Markdown handbook files
├── tests/                              # Backend validation and regression tests
├── RAG.ipynb                           # Kept untouched for experimentation
├── app_legacy.py                       # Reference backup of original app.py
├── .env.example                        # Template for environment configuration
├── .gitignore                          # Standard git excludes
└── README.md                           # Quickstart guide
```

---

## Local Setup

### 1. Prerequisites
Ensure you have Python 3.12+ installed.

### 2. Install Dependencies
Navigate to the root directory and install dependencies:
```bash
pip install -r backend/requirements.txt
```

### 3. Environment Configuration
Copy the `.env.example` file to `.env` and fill in your Groq API key:
```bash
cp .env.example .env
```
*(On Windows PowerShell, use: `Copy-Item .env.example .env`)*

Configure your `.env`:
```env
GROQ_API_KEY=gsk_...
```

---

## Ingesting Documents (Optional/Developer Fallback)

The ingestion pipeline can be run as a standalone task. By default, the backend will auto-ingest documents from `data/handbook-master/` into `dir3` at startup if the database is not found.

To run ingestion manually:
```bash
python -m backend.app.ingestion.pipeline
```

---

## Running the Backend

Start the FastAPI backend server using `uvicorn`. Run this command from the `backend/` directory or with `PYTHONPATH` set:

```bash
cd backend
python -m app.main
```
This will launch the server at `http://127.0.0.1:8500`.

---

## API Endpoints

### 1. Health Probe
* **Endpoint**: `GET /health`
* **Response**:
  ```json
  {"status": "healthy"}
  ```

### 2. Chat Assistant
* **Endpoint**: `GET /Chat-Assistant/`
* **Query Parameter**: `question` (string)
* **Response**:
  ```json
  {
    "response": "Generated response string...",
    "docs": [
      {
        "chunk": "Document content chunk...",
        "page": 1,
        "file": "path/to/file.md"
      }
    ]
  }
  ```

### 3. Document Ingestion / Upload
* **Endpoint**: `POST /documents/upload`
* **Content-Type**: `multipart/form-data`
* **Form Field**: `file` (binary)
* **Validation**:
  * File size: Max 50 MB (configurable via `MAX_FILE_SIZE_MB`)
  * Allowed extensions: `.pdf`, `.docx`, `.txt`, `.md`, `.csv`, `.xlsx`, `.json`
* **Example Request (cURL)**:
  ```bash
  curl -X POST "http://127.0.0.1:8500/documents/upload" \
       -H "accept: application/json" \
       -H "Content-Type: multipart/form-data" \
       -F "file=@/path/to/handbook.pdf"
  ```
* **Example Response**:
  ```json
  {
    "document_id": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "filename": "handbook.pdf",
    "file_type": "pdf",
    "num_chunks": 40,
    "status": "success",
    "message": "Document ingested successfully. 40 chunk(s) produced."
  }
  ```

---

## Supported File Formats & Ingestion Details

* **PDF (`.pdf`)**: Extracted page-by-page. Preserves page number metadata.
* **Word (`.docx`)**: Text extracted using `docx2txt`.
* **Spreadsheets (`.csv`, `.xlsx`)**: Ingested row-by-row to preserve structured semantics. The column names and sheet names (for Excel) are injected into the text representation and metadata of each chunk.
* **JSON (`.json`)**: Nested arrays/objects are flattened recursively into structured key-value formats, preventing massive text blob collapses.
* **Text / Markdown (`.txt`, `.md`)**: Raw text loaded and cleaned.

---

## Configuration Variables

You can configure the behavior of the server and the ingestion pipeline in the `.env` file:

| Variable | Description | Default |
|---|---|---|
| `GROQ_API_KEY` | Required API key for the Groq client | None |
| `EMBEDDING_MODEL_NAME` | HuggingFace embedding model ID | `all-MiniLM-L6-v2` |
| `GROQ_MODEL_ID` | Groq Llama/GPT model to use | `openai/gpt-oss-120b` |
| `VECTOR_DB_DIR` | Directory to read/write Chroma database | `dir3` |
| `COLLECTION_NAME` | Name of Chroma collection | `collection` |
| `DATA_DIR` | Directory for raw text source files | `data/handbook-master` |
| `UPLOAD_DIR` | Directory where uploaded files are stored | `data/uploads` |
| `CACHE_TIMEOUT` | Expiration time of cached responses (seconds) | `1800` |
| `MAX_CACHE_SIZE` | Maximum number of responses to keep in cache | `500` |
| `CHUNK_SIZE` | Target size of split document chunks (chars) | `500` |
| `CHUNK_OVERLAP` | Overlap size of split document chunks (chars) | `50` |
| `MAX_FILE_SIZE_MB` | Maximum permitted upload file size (MB) | `50` |
