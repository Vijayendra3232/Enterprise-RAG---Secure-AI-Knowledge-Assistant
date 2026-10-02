import os
from typing import Dict, Any
from dotenv import load_dotenv
from groq import Groq

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
        """
        load_dotenv()

        self.llm_params = llm_params or {}
        self.model_id = model_id

        # Load credentials from environment
        api_key = os.getenv("GROQ_API_KEY")

        if not api_key:
            self.client = None
            return

        try:
            self.client = Groq(api_key=api_key)
        except Exception as e:
            raise RuntimeError(f"Failed to initialize Groq client for model '{self.model_id}': {e}")

    def _translate_params(self) -> Dict[str, Any]:
        """
        Translate accepted llm_params into kwargs valid for
        Groq's client.chat.completions.create(...).
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

        return kwargs

    def generate_response(self, prompt: str) -> str:
        """
        Generate a response from the LLM for a given prompt.
        """
        if not self.client:
            raise EnvironmentError(
                "Missing required environment variable: GROQ_API_KEY. "
                "Ensure it is defined in your .env file."
            )

        import time
        from app.observability.tracing import tracer
        from app.observability.metrics import (
            llm_requests_total,
            llm_duration_seconds,
            llm_errors_total,
            llm_tokens_total,
        )
        from app.observability.schemas import LLMTelemetry

        t_start = time.perf_counter()
        with tracer.start_span("llm.generate", attributes={"llm.provider": "groq", "llm.model": self.model_id}) as span:
            try:
                kwargs = self._translate_params()
                completion = self.client.chat.completions.create(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    **kwargs,
                )
                duration_ms = (time.perf_counter() - t_start) * 1000

                prompt_tokens = getattr(getattr(completion, "usage", None), "prompt_tokens", None)
                completion_tokens = getattr(getattr(completion, "usage", None), "completion_tokens", None)
                total_tokens = getattr(getattr(completion, "usage", None), "total_tokens", None)

                llm_requests_total.inc(labels={"provider": "groq", "model": self.model_id, "status": "success"})
                llm_duration_seconds.observe(duration_ms / 1000.0, labels={"provider": "groq", "model": self.model_id})

                if prompt_tokens:
                    llm_tokens_total.inc(amount=float(prompt_tokens), labels={"provider": "groq", "token_type": "prompt"})
                if completion_tokens:
                    llm_tokens_total.inc(amount=float(completion_tokens), labels={"provider": "groq", "token_type": "completion"})

                return completion.choices[0].message.content
            except Exception as e:
                duration_ms = (time.perf_counter() - t_start) * 1000
                err_class = e.__class__.__name__
                llm_errors_total.inc(labels={"provider": "groq", "error_class": err_class})
                llm_requests_total.inc(labels={"provider": "groq", "model": self.model_id, "status": "error"})
                if span:
                    span.set_status("ERROR")
                raise RuntimeError(f"Error generating response from LLM: {e}")

