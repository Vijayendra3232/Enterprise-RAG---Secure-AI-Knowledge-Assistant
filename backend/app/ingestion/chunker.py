"""
chunker.py — Configurable text splitting for LangChain Documents.

Chunk size and overlap are read from environment variables (CHUNK_SIZE,
CHUNK_OVERLAP) via config, so they can be tuned without code changes.

The default splitter is RecursiveCharacterTextSplitter, which is the best
general-purpose choice for mixed prose + code + markdown content.
Metadata is preserved and propagated to every child chunk.
"""

from typing import List

from langchain_core.documents import Document
from langchain_text_splitters import (
    CharacterTextSplitter,
    RecursiveCharacterTextSplitter,
    TokenTextSplitter,
)

from app import config


def create_chunks(
    documents: List[Document],
    chunking_method: str = "RecursiveCharacterTextSplitter",
    chunk_size: int = None,
    chunk_overlap: int = None,
    separator: str = "\n",
) -> List[Document]:
    """
    Split a list of LangChain Documents into smaller chunks.

    Args:
        documents:       Input documents to split.
        chunking_method: One of "RecursiveCharacterTextSplitter" (default),
                         "CharacterTextSplitter", or "TokenTextSplitter".
        chunk_size:      Max characters per chunk.  Defaults to config.CHUNK_SIZE.
        chunk_overlap:   Overlap characters between consecutive chunks.
                         Defaults to config.CHUNK_OVERLAP.
        separator:       Used only by CharacterTextSplitter.

    Returns:
        List of chunked Documents with metadata inherited from the parent.
    """
    effective_size = chunk_size if chunk_size is not None else config.CHUNK_SIZE
    effective_overlap = chunk_overlap if chunk_overlap is not None else config.CHUNK_OVERLAP

    print(
        f"[Chunker] method={chunking_method}  "
        f"chunk_size={effective_size}  chunk_overlap={effective_overlap}"
    )

    if chunking_method == "CharacterTextSplitter":
        splitter = CharacterTextSplitter(
            separator=separator,
            chunk_size=effective_size,
            chunk_overlap=effective_overlap,
            length_function=len,
            add_start_index=True,
        )
    elif chunking_method == "TokenTextSplitter":
        splitter = TokenTextSplitter(
            chunk_size=effective_size,
            chunk_overlap=effective_overlap,
        )
    else:
        # Default — RecursiveCharacterTextSplitter
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=effective_size,
            chunk_overlap=effective_overlap,
            length_function=len,
            add_start_index=True,
        )

    chunks = splitter.split_documents(documents)
    print(f"[Chunker] {len(documents)} doc(s) -> {len(chunks)} chunks")
    return chunks
