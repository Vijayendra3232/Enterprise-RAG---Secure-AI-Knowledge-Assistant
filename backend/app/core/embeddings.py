import os
import functools
import logging
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer
from typing import List, Optional

logger = logging.getLogger(__name__)

# Constrain thread count for 1-vCPU memory-constrained environments
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


class PureONNXEmbeddings:
    """
    Production Pure ONNX + Rust tokenizers text embedding implementation.
    Eliminates PyTorch, transformers, and sentence-transformers runtime dependencies entirely.
    Executes attention-mask-aware mean pooling and L2 normalization over 384 dimensions.
    Constrained to 1 thread for 512 MB RAM environments.
    """

    def __init__(self, model_name_or_path: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self.model_name_or_path = model_name_or_path
        self.dimension = 384

        tokenizer_path, onnx_model_path = self._resolve_artifacts(model_name_or_path)

        logger.info(f"[ONNXEmbeddings] Loading tokenizer from: {tokenizer_path}")
        self.tokenizer = Tokenizer.from_file(tokenizer_path)
        self.tokenizer.enable_padding()
        self.tokenizer.enable_truncation(max_length=512)

        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = 1
        session_options.inter_op_num_threads = 1

        logger.info(f"[ONNXEmbeddings] Loading ONNX session from: {onnx_model_path}")
        self.session = ort.InferenceSession(
            onnx_model_path,
            session_options,
            providers=["CPUExecutionProvider"],
        )

        self.required_inputs = {i.name for i in self.session.get_inputs()}
        logger.info(f"[ONNXEmbeddings] Session inputs initialized: {self.required_inputs}")

    def _resolve_artifacts(self, name_or_path: str) -> tuple[str, str]:
        """
        Locate tokenizer.json and model.onnx artifacts locally or via HF hub.
        """
        if "/" not in name_or_path and not os.path.isabs(name_or_path) and not os.path.exists(name_or_path):
            name_or_path = f"sentence-transformers/{name_or_path}"

        if os.path.isdir(name_or_path):
            tok = os.path.join(name_or_path, "tokenizer.json")
            onnx = os.path.join(name_or_path, "onnx", "model.onnx")
            if not os.path.isfile(onnx):
                onnx = os.path.join(name_or_path, "model.onnx")
            if os.path.isfile(tok) and os.path.isfile(onnx):
                return tok, onnx

        # Check Hugging Face hub local cache directory
        hf_home = os.getenv("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
        repo_folder = f"models--{name_or_path.replace('/', '--')}"
        snapshots_dir = os.path.join(hf_home, "hub", repo_folder, "snapshots")

        if os.path.isdir(snapshots_dir):
            for snapshot_name in sorted(os.listdir(snapshots_dir), reverse=True):
                snap_path = os.path.join(snapshots_dir, snapshot_name)
                if os.path.isdir(snap_path):
                    tok = os.path.join(snap_path, "tokenizer.json")
                    onnx = os.path.join(snap_path, "onnx", "model.onnx")
                    if not os.path.isfile(onnx):
                        onnx = os.path.join(snap_path, "model.onnx")
                    if os.path.isfile(tok) and os.path.isfile(onnx):
                        return tok, onnx

        is_offline = (
            os.getenv("HF_HUB_OFFLINE", "0") == "1"
            or os.getenv("TRANSFORMERS_OFFLINE", "0") == "1"
            or os.getenv("ENVIRONMENT", "").lower() == "production"
            or os.getenv("APP_ENV", "").lower() == "production"
        )

        if is_offline:
            raise RuntimeError(
                f"[ONNXEmbeddings] CRITICAL: Required ONNX artifacts for '{name_or_path}' "
                f"missing from local cache ({snapshots_dir}) in offline/production mode."
            )

        # Download artifacts using huggingface_hub if online
        try:
            from huggingface_hub import hf_hub_download, snapshot_download
            logger.info(f"[ONNXEmbeddings] Downloading snapshot/onnx for '{name_or_path}'...")
            snapshot_download(repo_id=name_or_path)
            onnx_file = hf_hub_download(repo_id=name_or_path, filename="onnx/model.onnx")
            tok_file = hf_hub_download(repo_id=name_or_path, filename="tokenizer.json")
            return tok_file, onnx_file
        except Exception as exc:
            raise RuntimeError(
                f"[ONNXEmbeddings] Failed to download ONNX artifacts for '{name_or_path}': {exc}"
            ) from exc

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """
        Embed a list of text strings into 384-dimensional L2-normalized float vectors.
        """
        if not texts:
            return []

        # Sanitize texts (replace empty/whitespace with single space for numerical safety)
        sanitized_texts = [t if (t and t.strip()) else " " for t in texts]

        encoded = self.tokenizer.encode_batch(sanitized_texts)
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)

        ort_inputs = {}
        if "input_ids" in self.required_inputs:
            ort_inputs["input_ids"] = input_ids
        if "attention_mask" in self.required_inputs:
            ort_inputs["attention_mask"] = attention_mask
        if "token_type_ids" in self.required_inputs:
            token_type_ids = np.array([e.type_ids for e in encoded], dtype=np.int64)
            ort_inputs["token_type_ids"] = token_type_ids

        outputs = self.session.run(None, ort_inputs)
        last_hidden_state = outputs[0]  # shape [batch_size, seq_len, 384]

        # Attention-mask-aware mean pooling
        input_mask_expanded = np.expand_dims(attention_mask, -1).astype(np.float32)
        sum_embeddings = np.sum(last_hidden_state * input_mask_expanded, axis=1)
        sum_mask = np.clip(input_mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
        mean_pooled = sum_embeddings / sum_mask

        # L2 normalization
        norms = np.linalg.norm(mean_pooled, axis=1, keepdims=True)
        normalized = mean_pooled / np.maximum(norms, 1e-9)

        result = normalized.tolist()

        # Explicit memory cleanup of transient numpy arrays
        del input_ids
        del attention_mask
        del ort_inputs
        del outputs
        del last_hidden_state
        del mean_pooled
        del normalized

        return result

    def embed_query(self, text: str) -> List[float]:
        """
        Embed a single search query text into a 384-dimensional L2-normalized float vector.
        """
        vecs = self.embed_documents([text])
        return vecs[0] if vecs else [0.0] * self.dimension

    def __call__(self, texts: List[str]) -> List[List[float]]:
        """Callable interface compatibility for LangChain / Chroma."""
        return self.embed_documents(texts)

    def get_sentence_embedding_dimension(self) -> int:
        """Return sentence embedding vector dimension (384)."""
        return self.dimension


@functools.lru_cache(maxsize=4)
def load_embedding_model(embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
    """
    Loads and caches a PureONNXEmbeddings instance.
    Optimized for 1-vCPU 512 MB memory-constrained environments with singleton caching.
    Enforces local ONNX artifacts in production / offline mode.
    """
    model = PureONNXEmbeddings(model_name_or_path=embedding_model_name)
    logger.info(f"Loaded and cached Pure ONNX embedding model: {embedding_model_name}")
    return model


def get_embedding_dimension(embedding_model_or_name) -> int:
    """
    Dynamically resolve the vector dimension of the active embedding model.
    """
    model = embedding_model_or_name
    if isinstance(model, str):
        model = load_embedding_model(model)

    if hasattr(model, "get_sentence_embedding_dimension"):
        dim = model.get_sentence_embedding_dimension()
        if dim and isinstance(dim, int) and dim > 0:
            return dim
    sample_vec = model.embed_query("dimension_probe")
    return len(sample_vec)
