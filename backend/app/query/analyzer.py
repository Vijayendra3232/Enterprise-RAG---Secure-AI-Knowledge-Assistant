import json
import re
from typing import Dict, Any, List, Optional
from app import config
from app.core.llm import LLM
from app.query.models import QueryAnalysis

class QueryAnalyzer:
    """
    Analyzes user queries to determine classification, complexity, intent,
    and whether they require query rewriting or decomposition.
    """
    def __init__(self, llm_model_id: str = config.GROQ_MODEL_ID):
        self.llm_id = llm_model_id
        self.llm_params = {
            "decoding_method": "greedy",
            "max_new_tokens": 500,
            "min_new_tokens": 0,
        }
        self.llm_obj = None
        if config.GROQ_API_KEY:
            try:
                self.llm_obj = LLM(self.llm_params, self.llm_id)
            except Exception as e:
                print(f"[QueryAnalyzer] Warning: failed to initialize LLM: {e}")

    def analyze(self, query: str) -> QueryAnalysis:
        """
        Main query analysis entry point. Determines classification deterministically
        where possible, falling back to a single LLM call for semantic complexity.
        """
        query_stripped = query.strip()
        
        # ── Fast-path Rule 1: Empty or trivial ──────────────────────────────
        if not query_stripped:
            return QueryAnalysis(original_query=query)

        # ── Fast-path Rule 2: Conversational Greetings / Simple ─────────────
        conversational_patterns = [
            r"^(hello|hi|hey|greetings|good morning|good afternoon|good evening|thanks|thank you)\b"
        ]
        if any(re.search(pattern, query_stripped.lower()) for pattern in conversational_patterns):
            if len(query_stripped) < 40:
                return QueryAnalysis(
                    original_query=query,
                    intent="conversational",
                    complexity="simple",
                    query_type="conversational",
                    needs_rewriting=False,
                    needs_decomposition=False,
                    needs_expansion=False
                )

        # ── Heuristic Checks: Ambiguity, Conjunctions & Complexity ─────────
        # Check ambiguous keywords only as signals
        ambiguous_keywords = ["thing", "stuff", "what about", "how about"]
        has_ambiguous_keyword = any(k in query_stripped.lower() for k in ambiguous_keywords)
        is_short = len(query_stripped) < 40

        # Check comparison language & multi-part indicators
        conjunctions = [" and ", " or ", " vs ", " versus ", " but ", " compare "]
        has_conjunction = any(c in query_stripped.lower() for c in conjunctions)
        
        # Question clauses / clauses count helper
        split_clauses = [c.strip() for c in re.split(r'[?,;.]', query_stripped) if c.strip()]
        has_multiple_clauses = len(split_clauses) > 1

        # Check if query contains procedural action verbs or starts with "how"
        procedural_keywords = ["how do i", "how can i", "how to", "process for", "procedure for", "apply for", "request"]
        is_procedural_candidate = any(kw in query_stripped.lower() for kw in procedural_keywords)

        # Check for vague or domain-specific conversational phrasing requiring LLM rewrite
        vague_conversational_keywords = ["take off", "time off", "how long", "what about", "how about", "thing", "stuff"]
        has_vague_keywords = any(kw in query_stripped.lower() for kw in vague_conversational_keywords)

        # Determine if we should route to LLM fallback
        # Simple factual check: If it has no conjunctions, no ambiguous words, single clause, is short, not procedural, and not vague
        is_deterministic_simple = (
            not has_conjunction and 
            not has_multiple_clauses and 
            not has_ambiguous_keyword and 
            not is_procedural_candidate and
            not has_vague_keywords and
            len(query_stripped) < 55
        )

        if is_deterministic_simple:
            # Safe simple factual path (no LLM call)
            return QueryAnalysis(
                original_query=query,
                intent="factual",
                complexity="simple",
                query_type="single_hop",
                needs_rewriting=False,
                needs_decomposition=False,
                needs_expansion=False
            )

        # ── Fallback Path: Unified Semantic LLM Analysis ─────────────────────
        if not self.llm_obj:
            # Fail-safe if LLM is offline or unconfigured
            return self._fail_safe(query)

        prompt = self._build_analysis_prompt(query_stripped)
        try:
            response = self.llm_obj.generate_response(prompt)
            # Clean JSON formatting wrappers
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json|python)?|```$", "", cleaned, flags=re.MULTILINE).strip()
            
            data = json.loads(cleaned)
            
            # Map sub-queries with limit
            sub_queries = [str(sq).strip() for sq in data.get("sub_queries", []) if str(sq).strip()]
            
            return QueryAnalysis(
                original_query=query,
                intent=str(data.get("intent", "factual")),
                complexity=str(data.get("complexity", "simple")),
                query_type=str(data.get("query_type", "single_hop")),
                needs_rewriting=bool(data.get("needs_rewriting", False)),
                needs_decomposition=bool(data.get("needs_decomposition", False)),
                needs_expansion=bool(data.get("needs_expansion", False)),
                rewritten_query=data.get("rewritten_query"),
                sub_queries=sub_queries
            )

        except Exception as e:
            print(f"[QueryAnalyzer] Semantic analysis failed: {e}. Falling back to safe path.")
            return self._fail_safe(query)

    def _fail_safe(self, query: str) -> QueryAnalysis:
        """Heuristic fallback when semantic parser fails or is disabled."""
        return QueryAnalysis(
            original_query=query,
            intent="unknown",
            complexity="simple",
            query_type="single_hop",
            needs_rewriting=False,
            needs_decomposition=False,
            needs_expansion=False
        )

    def _build_analysis_prompt(self, query: str) -> str:
        return f"""
You are an expert query understanding module for a corporate employee handbook.
Analyze the user's question and classify its properties, intent, complexity, type, and whether it requires query rewriting or decomposition.

Format the output strictly as a single JSON object. No explanations, no comments, no markdown code blocks.

Schema:
{{
  "intent": "factual" | "procedural" | "comparison" | "summarization" | "analytical" | "conversational" | "unknown",
  "complexity": "simple" | "moderate" | "complex",
  "query_type": "single_hop" | "multi_hop" | "multi_part" | "ambiguous" | "structured" | "conversational",
  "needs_rewriting": true | false,
  "needs_decomposition": true | false,
  "needs_expansion": true | false,
  "rewritten_query": "Clear search query focusing on policy lookup terms without pronouns or conversational filler",
  "sub_queries": ["sub query 1?", "sub query 2?"]
}}

Rules:
1. Set "needs_rewriting" to true if the question is vague, conversational, or uses pronouns without context (e.g. "What about the leave thing?").
2. Set "needs_decomposition" to true for multi-part, compound questions (e.g. "What is our vacation policy and how does it compare to sabbatical?"). Generate the distinct lookup questions in "sub_queries".
3. For simple factual questions (e.g. "What is the sabbatical policy?"), keep complexity simple and needs_rewriting/decomposition false.
4. "rewritten_query" must be a clean search phrase. Do NOT answer the question.

Analyze query: "{query}"
JSON:
"""
