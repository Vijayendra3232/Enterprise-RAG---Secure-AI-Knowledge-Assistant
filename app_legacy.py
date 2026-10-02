#Import Libraries and functions
import ast
import re

import uvicorn
import chromadb
import pandas as pd
import time
import threading
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
import datetime
from langchain_core.documents import Document
from dotenv import load_dotenv
from fastapi.responses import FileResponse
from typing import List, Dict, Any
import os
import numpy as np
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import CharacterTextSplitter, TokenTextSplitter, RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma, FAISS
from langchain_community.retrievers import BM25Retriever
from langchain_classic.retrievers import EnsembleRetriever
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.utils.math import cosine_similarity
from sentence_transformers import CrossEncoder
from langchain_community.embeddings import SentenceTransformerEmbeddings
from groq import Groq


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust this to a specific origin or origins if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
load_dotenv()

# Cache format: { question_text: (answer, docs, last_invocation_time) }
cache = {}
CACHE_TIMEOUT = 1800  # 30 mins
cache_lock = threading.Lock()

def monitor_cache_timeout():
    """
    Monitor cache timeout and remove entries older than CACHE_TIMEOUT
    """
    while True:
        current_time = time.time()

        with cache_lock:
            for question, data in list(cache.items()):
                _, _, last_time = data
                if current_time - last_time > CACHE_TIMEOUT:
                    print(f"[CACHE] Entry expired: {question}")
                    cache.pop(question, None)

        time.sleep(60)  # run cleanup every minute


# Start the timer monitor thread
timer_thread = threading.Thread(target=monitor_cache_timeout)
timer_thread.daemon = True  # Daemonize the thread so it will exit when the main thread exits
timer_thread.start()

class LLM:
    """
    Wrapper class for interacting with a Large Language Model (LLM)
    hosted on Groq.

    Responsibilities:
    1. Load the Groq API key from the environment.
    2. Initialize the Groq client with model parameters.
    3. Generate text responses for a given prompt.
    """

    def __init__(self, llm_params: Dict[str, Any] = None, model_id: str = "openai/gpt-oss-120b"):
        """
        Initialize the LLM model with a Groq API key.

        Args:
            llm_params (dict, optional): Model generation parameters. Accepts either
                Groq-native keys (temperature, max_tokens, top_p, stop, frequency_penalty,
                presence_penalty, seed) or the old watsonx-style keys this class used to take
                (decoding_method, min_new_tokens, max_new_tokens, repetition_penalty,
                stop_sequences) which are translated automatically for backward compatibility.
            model_id (str, optional): Groq model ID to use. Defaults to 'openai/gpt-oss-120b'
                (Groq's recommended replacement for the now-deprecated llama-3.3-70b-versatile,
                which is being decommissioned around August 2026 — see
                https://console.groq.com/docs/deprecations).
        """
        load_dotenv()

        self.llm_params = llm_params or {}
        self.model_id = model_id

        # Load credentials from environment
        api_key = os.getenv("GROQ_API_KEY")

        # Validate credentials
        if not api_key:
            raise EnvironmentError(
                "Missing required environment variable: GROQ_API_KEY. "
                "Ensure it is defined in your .env file."
            )

        try:
            self.client = Groq(api_key=api_key)
        except Exception as e:
            raise RuntimeError(f"Failed to initialize Groq client for model '{self.model_id}': {e}")

    # ------------------------------------------------------------------
    # Param translation (watsonx-style -> Groq chat.completions kwargs)
    # ------------------------------------------------------------------
    def _translate_params(self) -> Dict[str, Any]:
        """
        Translate accepted llm_params into kwargs valid for
        Groq's client.chat.completions.create(...).

        Groq has no equivalent for watsonx's min_new_tokens or repetition_penalty,
        so those are dropped rather than approximated.
        """
        p = self.llm_params
        kwargs: Dict[str, Any] = {}

        # decoding_method="greedy" -> deterministic sampling
        if p.get("decoding_method") == "greedy":
            kwargs["temperature"] = 0.0
        elif "temperature" in p:
            kwargs["temperature"] = p["temperature"]

        if "max_new_tokens" in p:
            kwargs["max_tokens"] = p["max_new_tokens"]
        elif "max_tokens" in p:
            kwargs["max_tokens"] = p["max_tokens"]

        if "stop_sequences" in p:
            kwargs["stop"] = p["stop_sequences"]
        elif "stop" in p:
            kwargs["stop"] = p["stop"]

        if "top_p" in p:
            kwargs["top_p"] = p["top_p"]
        if "frequency_penalty" in p:
            kwargs["frequency_penalty"] = p["frequency_penalty"]
        if "presence_penalty" in p:
            kwargs["presence_penalty"] = p["presence_penalty"]
        if "seed" in p:
            kwargs["seed"] = p["seed"]

        # Unsupported by Groq's API, silently dropped: min_new_tokens, repetition_penalty, top_k
        return kwargs

    # ------------------------------------------------------------------
    # Response Generation
    # ------------------------------------------------------------------
    def generate_response(self, prompt: str) -> str:
        """
        Generate a response from the LLM for a given prompt.

        NOTE: `prompt` here is the fully pre-formatted Llama-style string built by
        `Prompt.get_prompt(...)` (including <|begin_of_text|> etc.). Groq only exposes
        a chat-completions endpoint, not a raw-completion endpoint, so this string is
        sent as a single user message rather than as raw completion input. The model
        will generally still produce a sensible answer, but for best results consider
        refactoring to pass SYSTEM_PROMPT / USER_PROMPT as separate role="system" /
        role="user" messages instead of one pre-templated block.

        Args:
            prompt (str): The text prompt to send to the model.

        Returns:
            str: The generated response text.
        """
        try:
            kwargs = self._translate_params()
            completion = self.client.chat.completions.create(
                model=self.model_id,
                messages=[{"role": "user", "content": prompt}],
                **kwargs,
            )
            return completion.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"Error generating response from LLM: {e}")

def load_embedding_model(embedding_model_name: str):
    """
    This function loads a pre-trained text embedding model that can be used for various downstream  tasks.
    It provides a convenient interface for accessing the text embedding functionality.

    Args:
        embedding_model_name (str): Name of the text embedding model to be used.
                                    It specifies the pre-trained model that will be loaded or instantiated for text embedding task.
    Returns:
            The loaded text embedding model instance ready for use in downstream tasks.

    """
    # NOTE: SentenceTransformerEmbeddings (langchain_community.embeddings) is
    # deprecated. HuggingFaceEmbeddings (langchain_huggingface) is the
    # maintained replacement and accepts the same constructor shape.
    embedding_model = HuggingFaceEmbeddings(model_name=embedding_model_name, model_kwargs={"trust_remote_code": True})
    print(f"Loaded text embedding model: {embedding_model_name}")
    return embedding_model

def load_vector_store(persist_dir: str, collection_name: str, embedding_model_name: str = "all-MiniLM-L6-v2"):
        """Load an existing vector store.
        Args:
            persist_directory (list): Directory where the vector store data is located
            collection_name (str): Unique name to assign to vector store
            embedding_model : The text embedding model
        """

        client = chromadb.PersistentClient(persist_dir)
        colls = [c.name for c in client.list_collections()]

        if not collection_name in colls:
            msg = (f"The collection {collection_name} does not exist."
                   f" in the supplied directory. Available collections: {colls}")
            raise Exception(msg)
        embedding_model = load_embedding_model(embedding_model_name)
        # NOTE: passing persist_directory here makes Chroma spin up its own
        # internal client, separate from the `client` object built above.
        # With modern chromadb versions this legacy path is fragile — pass
        # the already-constructed PersistentClient explicitly instead.
        vectordb = Chroma(client=client,
                          embedding_function=embedding_model,
                          collection_name=collection_name)
        print(f'Loaded vector store {vectordb._collection.name} containing {vectordb._collection.count()} entries...')
        return vectordb

class Prompt:

    def __init__(self, system_prompt, user_prompt):
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt

    def get_prompt(self, prompt_struct, context, question):
        self.user_prompt = self.user_prompt.format(CONTEXT=context, QUESTION=question)
        return prompt_struct.format(SYSTEM_PROMPT=self.system_prompt, USER_PROMPT=self.user_prompt)

SYSTEM_PROMPT = """You are a helpful, respectful and honest assistant.
Your answers should not include any harmful, unethical, racist, sexist, toxic, dangerous, or illegal content.
Please ensure that your responses are socially unbiased and positive in nature.
If you don't know the answer to a question, please don't share false information."""

USER_PROMPT = """Read the context and answer the question.
If it cannot be answered, only say: 'Unanswerable'.
Answer should be concise and professional.
Make sure response is not cut off, and do not give an empty response.
Guidelines for Answering:
1. Understand the Context
2. Base answers solely on the information within the given context; do not rely on external knowledge.
3. Craft responses in full sentences to enhance clarity.
4. Be Concise and Relevant. Avoid unnecessary elaboration.
5. Provide answers without personal opinions or interpretations.
6. Keep your response format consistent, adapting it to fit the nature of the question
7. Rely solely on the provided context. Do not introduce external information.
8. Only Respond in the language of the question. Ensure that the answer is provided in the same language as the question, unless otherwise specified. So therefore, if a question is given in Spanish, you have to answer in Spanish
#### START CONTEXT
Context:
{CONTEXT}
#### END CONTEXT
Question:
{QUESTION}
Answer: """

prompt_struct = """
<|begin_of_text|><|start_header_id|>system<|end_header_id|>
{SYSTEM_PROMPT}<|eot_id|><|start_header_id|>user<|end_header_id|>
{USER_PROMPT}<|eot_id|><|start_header_id|>assistant<|end_header_id|>
"""

vectordb = load_vector_store(persist_dir="dir3", collection_name="collection")

llm_params = {
            'decoding_method': "greedy",
            'min_new_tokens': 1,
            'max_new_tokens': 500,
            'repetition_penalty': 1.1,
            # "temperature": 0.2,
            # "top_k":50,
            # "top_p":1
            }
llm_obj = LLM(llm_params=llm_params, model_id='openai/gpt-oss-120b')


class RAGFusion:
    """
    Implements the RAG Fusion process using multiple query generation
    and Reciprocal Rank Fusion (RRF) for improved document retrieval.

    Workflow:
    1. **Query Generation**: Generate multiple semantically similar queries from the original input using an LLM.
    2. **Vector Search**: Perform vector-based search for each generated query to fetch top-matching documents.
    3. **Reciprocal Rank Fusion (RRF)**: Re-rank retrieved documents based on their occurrence and ranking across queries.
    4. **Metadata Enrichment**: Combine reranked results with metadata (e.g., source, page).
    5. **Output**: Return the reranked and metadata-enriched document list.
    """

    def __init__(self):
        """Initialize model configuration and LLM instance."""
        self.llm_id = "openai/gpt-oss-120b"
        self.llm_params = {
        "decoding_method": "greedy",
        "max_new_tokens": 800,
        "min_new_tokens": 0,
        }
        self.llm_obj = LLM(self.llm_params, self.llm_id)

    # ------------------------------------------------------------------
    # Query Generation
    # ------------------------------------------------------------------
    def _build_query_prompt(self, base_query: str, n_queries: int) -> str:
        """
        Build an instruction prompt for generating semantically similar queries.

        Args:
            base_query (str): The original user query.
            n_queries (int): Number of alternate queries to generate.

        Returns:
            str: The formatted prompt for the LLM.
        """
        return f"""
<|begin_of_text|><|start_header_id|>system<|end_header_id|>
You are an assistant that generates {n_queries} unique, semantically equivalent search queries.<|eot_id|><|start_header_id|>user<|end_header_id|>
Instructions:
- Each query should preserve the original intent and meaning.
- Output should be a valid Python list: ["...", "...", "..."]
- Each query must be enclosed in double quotes.
- Avoid duplicates or near-duplicates.
- Return only the Python list — no explanations.

Example:
Question: "How do I integrate IBM Watson services into my application?"
Expected Output:
["What steps are involved in incorporating IBM Watson services into an application?",
 "Can you provide guidance on integrating IBM Watson into an app?",
 "How can I integrate IBM Watson functionalities into my application?"]

Now generate {n_queries} alternate search queries for:
Original Question: "{base_query}"
<|eot_id|><|start_header_id|>assistant<|end_header_id|>
"""

    def generate_queries(self, base_query: str, n_queries: int) -> list[str]:
        """
        Generate multiple semantically similar queries from the original one using LLM.

        Args:
            base_query (str): The original user query.
            n_queries (int): Number of queries to generate.

        Returns:
            list[str]: List of generated query strings.
        """
        prompt = self._build_query_prompt(base_query, n_queries)
        response = self.llm_obj.generate_response(prompt)

        # Clean and evaluate list safely
        raw_response = response
        response = response.replace("Output:", "").strip()
        response = re.sub(r"^```(?:python|json)?|```$", "", response, flags=re.MULTILINE).strip()

        try:
            generated_queries = ast.literal_eval(response)
            if not isinstance(generated_queries, list):
                raise ValueError("Parsed output is not a list.")
        except Exception:
            print(f"[RAGFusion] Failed to parse LLM output as list. Raw response was:\n{raw_response}")
            raise ValueError(f"Failed to parse generated queries from LLM output. Raw: {raw_response[:200]}")

        return generated_queries

    # ------------------------------------------------------------------
    # Reciprocal Rank Fusion (RRF)
    # ------------------------------------------------------------------
    def _reciprocal_rank_fusion(self, results_dict: dict, k: int = 60) -> dict:
        """
        Apply Reciprocal Rank Fusion (RRF) to merge results from multiple queries.

        Args:
            results_dict (dict): {query: {doc_content: [source, score], ...}, ...}
            k (int, optional): RRF constant to smooth the rank contribution. Defaults to 60.

        Returns:
            dict: {doc_content: fused_score}
        """
        fused_scores = {}
        for query, doc_map in results_dict.items():
            sorted_docs = sorted(doc_map.items(), key=lambda x: x[1][2], reverse=True)
            for rank, (doc, _) in enumerate(sorted_docs):
                fused_scores[doc] = fused_scores.get(doc, 0) + 1 / (rank + k)
        return dict(sorted(fused_scores.items(), key=lambda x: x[1], reverse=True))

    # ------------------------------------------------------------------
    # Metadata Enrichment
    # ------------------------------------------------------------------
    def _merge_metadata(self, all_results: dict, fused_scores: dict) -> dict:
        """
        Attach metadata (e.g., source, page) to fused documents.

        Args:
            all_results (dict): {query: {doc_content: [source, page, score]}}
            fused_scores (dict): {doc_content: fused_score}

        Returns:
            dict: {doc_content: [source, page, score, fused_score]}
        """
        combined_metadata = {
            doc: meta for query_res in all_results.values() for doc, meta in query_res.items()
        }

        for doc, fused_score in fused_scores.items():
            if doc in combined_metadata:
                combined_metadata[doc].append(fused_score)
        return combined_metadata

    # ------------------------------------------------------------------
    # RAG Fusion Retrieval Orchestration
    # ------------------------------------------------------------------
    def run(self, query: str, vector_db, n_queries: int = 2, n_retrieve: int = 4) -> list[str]:
        """
        Execute the full RAG Fusion pipeline.

        Steps:
        1. Generate semantically similar queries.
        2. Perform vector search for each query.
        3. Apply Reciprocal Rank Fusion to merge results.
        4. Merge metadata and rerank.
        5. Return ordered document contents.

        Args:
            query (str): The user's original query.
            vector_db: The vector database object supporting `similarity_search_with_score`.
            n_queries (int, optional): Number of queries to generate. Defaults to 2.
            n_retrieve (int, optional): Documents to retrieve per query. Defaults to 4.

        Returns:
            list[str]: Ranked document contents after RAG Fusion.
        """
        all_results = {}

        # Generate expanded queries
        expanded_queries = [query] + self.generate_queries(query, n_queries)
        docs = []
        # Retrieve results for each generated query
        for q in expanded_queries:
            results = vector_db.similarity_search_with_score(q, n_retrieve)
            # print(results)
            all_results[q] = {
                r[0].page_content: [r[0].metadata["page"], r[0].metadata["file_path"], r[1]]
                for r in results
            }

        # Apply reciprocal rank fusion
        fused_scores = self._reciprocal_rank_fusion(all_results)

        # Attach metadata and reorder results
        reranked = self._merge_metadata(all_results, fused_scores)

        # for i in reranked:
        docs = []
        for chunk in reranked:
            d = {}
            d["chunk"] = chunk
            d["page"] = reranked[chunk][0]
            d["file"] = reranked[chunk][1]
            docs.append(d)

        return list(reranked.keys()), docs


@app.get("/Chat-Assistant/")
async def chat_assistant(question: str):
    if not question or not question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    current_time = time.time()

    # ------------------ CACHE HIT -------------------
    with cache_lock:
        if question in cache:
            answer, docs, _ = cache[question]
            # update last access time
            cache[question] = (answer, docs, current_time)
            print("[CACHE] Hit:", question)
            return {"response": answer, "docs": docs}

    # ------------------ CACHE MISS ------------------
    print("[CACHE] Miss:", question)

    try:
        RF_obj = RAGFusion()
        RF_context, docs = RF_obj.run(question, vectordb)

        p_obj = Prompt(SYSTEM_PROMPT, USER_PROMPT)
        prompt = p_obj.get_prompt(prompt_struct, RF_context, question)
        answer = llm_obj.generate_response(prompt)

        # Save into cache
        with cache_lock:
            cache[question] = (answer, docs, current_time)
            MAX_CACHE_SIZE = 500
            if len(cache) > MAX_CACHE_SIZE:
                cache.pop(next(iter(cache)))

        return {"response": answer, "docs": docs}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8500, log_level="info")