import ast
import re
from typing import List, Dict, Any, Tuple
from app import config
from app.core.llm import LLM
from app.retrieval.models import SearchResult

def reciprocal_rank_fusion(ranked_lists: List[List[SearchResult]], k: int = config.RRF_K) -> List[SearchResult]:
    """
    Applies Reciprocal Rank Fusion (RRF) to merge multiple ranked SearchResult lists
    into a single ranked list. RRF score formula:
        score = sum(1 / (k + rank))
    where rank is 1-indexed.
    """
    rrf_scores: Dict[str, float] = {}
    chunk_docs: Dict[str, SearchResult] = {}

    for rank_list in ranked_lists:
        for rank, res in enumerate(rank_list):
            cid = res.chunk_id
            if not cid:
                continue

            if cid not in chunk_docs:
                chunk_docs[cid] = res

            # Calculate RRF score contribution (1-indexed rank)
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + 1.0 / (k + (rank + 1))

    # Convert back to SearchResult list sorted by RRF score descending
    fused_results = []
    for cid, score in rrf_scores.items():
        base_res = chunk_docs[cid]
        res_copy = base_res.model_copy()
        res_copy.score = score

        meta = res_copy.metadata.copy() if res_copy.metadata else {}
        meta["rrf_score"] = score
        res_copy.metadata = meta

        fused_results.append(res_copy)

    fused_results.sort(key=lambda x: x.score, reverse=True)
    return fused_results


class QueryExpander:
    """
    Handles LLM-based query expansion to generate semantically equivalent queries.
    """
    def __init__(self, llm_model_id: str = config.GROQ_MODEL_ID):
        self.llm_id = llm_model_id
        self.llm_params = {
            "decoding_method": "greedy",
            "max_new_tokens": 800,
            "min_new_tokens": 0,
        }
        self.llm_obj = None
        # Safe initialization to avoid crashes in non-llm contexts (e.g. testing)
        if config.GROQ_API_KEY:
            try:
                self.llm_obj = LLM(self.llm_params, self.llm_id)
            except Exception as e:
                print(f"[QueryExpander] Warning: failed to initialize LLM: {e}")

    def _build_query_prompt(self, base_query: str, n_queries: int) -> str:
        """Instruction prompt for generating alternative search queries."""
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

    def expand(self, base_query: str, n_queries: int = 2) -> List[str]:
        """Generate alternate queries. Returns a list including the original query."""
        if not self.llm_obj:
            # Fallback if LLM is unavailable
            return [base_query]

        prompt = self._build_query_prompt(base_query, n_queries)
        try:
            response = self.llm_obj.generate_response(prompt)
            raw_response = response
            response = response.replace("Output:", "").strip()
            response = re.sub(r"^```(?:python|json)?|```$", "", response, flags=re.MULTILINE).strip()

            generated_queries = ast.literal_eval(response)
            if isinstance(generated_queries, list):
                # Clean elements and prefix with original
                cleaned = [q.strip() for q in generated_queries if isinstance(q, str) and q.strip()]
                return [base_query] + cleaned
        except Exception as e:
            print(f"[QueryExpander] Failed to parse generated queries: {e}")
            
        return [base_query]
