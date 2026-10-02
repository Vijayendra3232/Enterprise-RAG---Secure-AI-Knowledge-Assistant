import json
import re
from typing import Dict, Any, Tuple, List, Optional
from app import config
from app.core.llm import LLM
from app.generation.prompts import GROUNDED_SYSTEM_PROMPT, GROUNDED_USER_PROMPT

class GroundedGenerator:
    """
    Handles LLM grounded generation. Submits structured prompts to the LLM
    and parses responses into text draft answers and structured evidence claims.
    """
    def __init__(self, llm_model_id: str = config.GROQ_MODEL_ID):
        self.llm_id = llm_model_id
        self.llm_params = {
            "decoding_method": "greedy",
            "max_new_tokens": 800,
            "min_new_tokens": 1,
        }
        self.llm_obj = LLM(self.llm_params, self.llm_id)

    def generate(self, question: str, organized_context: List[str]) -> Tuple[str, List[Dict[str, Any]], bool]:
        """
        Submits prompt to LLM and returns (draft_answer, claims, refusal_triggered).
        """
        # Format the context block with citation tags
        formatted_context = ""
        for idx, chunk in enumerate(organized_context):
            formatted_context += f"[C{idx + 1}]\n{chunk}\n\n"

        prompt = (
            f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{GROUNDED_SYSTEM_PROMPT}<|eot_id|>\n"
            f"<|start_header_id|>user<|end_header_id|>\n"
            f"{GROUNDED_USER_PROMPT.format(context=formatted_context, question=question)}<|eot_id|>\n"
            f"<|start_header_id|>assistant<|end_header_id|>\n"
        )

        try:
            response = self.llm_obj.generate_response(prompt).strip()
            
            # Clean JSON wrappers if any
            if response.startswith("```"):
                response = re.sub(r"^```(?:json|python)?|```$", "", response, flags=re.MULTILINE).strip()

            data = json.loads(response)
            
            answer = str(data.get("answer", "")).strip()
            claims = data.get("claims", [])
            refusal = bool(data.get("refusal", False))
            
            # Check if answer implies refusal or no info
            refusal_indicators = ["i don't know", "insufficient information", "not enough information"]
            if any(ind in answer.lower() for ind in refusal_indicators):
                refusal = True
                
            return answer, claims, refusal

        except Exception as e:
            # Safe fallback if JSON formatting is violated by the model
            print(f"[GroundedGenerator] Warning: structured JSON parsing failed: {e}. Falling back to raw response.")
            raw_response = self.llm_obj.generate_response(prompt).strip()
            
            # Simple check if raw response implies refusal
            refusal = "insufficient" in raw_response.lower() or "don't know" in raw_response.lower()
            return raw_response, [], refusal
