import os
import functools

# Constrain PyTorch / OpenMP / MKL / Tokenizers thread count for 1-vCPU memory-constrained environments
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

try:
    import torch
    torch.set_num_threads(1)
except Exception:
    pass

from langchain_huggingface import HuggingFaceEmbeddings


@functools.lru_cache(maxsize=4)
def load_embedding_model(embedding_model_name: str):
    """
    Loads and caches a pre-trained text embedding model instance.
    Uses HuggingFaceEmbeddings (the maintained replacement for SentenceTransformerEmbeddings).
    Optimized for 1-vCPU memory-constrained environments with singleton caching.
    Enforces local_files_only in offline mode or production environment to guarantee
    zero runtime network downloads.
    """
    try:
        import torch
        torch.set_num_threads(1)
    except Exception:
        pass

    model_kwargs = {"trust_remote_code": True}
    if (
        os.getenv("HF_HUB_OFFLINE", "0") == "1"
        or os.getenv("TRANSFORMERS_OFFLINE", "0") == "1"
        or os.getenv("ENVIRONMENT", "").lower() == "production"
        or os.getenv("APP_ENV", "").lower() == "production"
    ):
        model_kwargs["local_files_only"] = True

    embedding_model = HuggingFaceEmbeddings(
        model_name=embedding_model_name,
        model_kwargs=model_kwargs,
        encode_kwargs={"batch_size": 16, "normalize_embeddings": True},
    )
    print(f"Loaded and cached text embedding model: {embedding_model_name}")
    return embedding_model



def get_embedding_dimension(embedding_model_or_name) -> int:
    """
    Dynamically resolve the vector dimension of the active embedding model.
    Never hardcode dimension assumptions (e.g. 384, 768, 1536).
    """
    model = embedding_model_or_name
    if isinstance(model, str):
        model = load_embedding_model(model)

    if hasattr(model, "client") and hasattr(model.client, "get_sentence_embedding_dimension"):
        dim = model.client.get_sentence_embedding_dimension()
        if dim and isinstance(dim, int) and dim > 0:
            return dim
    # Fallback: probe the model dynamically with a sample query
    sample_vec = model.embed_query("dimension_probe")
    return len(sample_vec)


