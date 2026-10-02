from langchain_huggingface import HuggingFaceEmbeddings

def load_embedding_model(embedding_model_name: str):
    """
    Loads a pre-trained text embedding model.
    Uses HuggingFaceEmbeddings (the maintained replacement for SentenceTransformerEmbeddings).
    """
    embedding_model = HuggingFaceEmbeddings(
        model_name=embedding_model_name,
        model_kwargs={"trust_remote_code": True}
    )
    print(f"Loaded text embedding model: {embedding_model_name}")
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


