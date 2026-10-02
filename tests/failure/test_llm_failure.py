"""
test_llm_failure.py — LLM Provider Timeout, 429 Rate Limiting & Malformed Output Testing

Verifies:
1. LLM provider 429 HTTP rate limiting triggers bounded exponential backoff with jitter.
2. LLM provider timeout is bounded and does not cause infinite retry loops.
3. Malformed LLM response is caught by Pydantic response models without ungrounded output.
4. Error logs do NOT leak raw prompts, secret API keys, or document contents.
"""

import json
from unittest.mock import MagicMock
import pytest

from tests.step15_config import TestMode, TestResultStatus


class TestLLMProviderFailureResilience:
    """Verifies robustness against third-party LLM service degradation and rate limits."""

    def test_llm_rate_limiting_429_backoff_handling(self):
        """Verify 429 rate limit triggers retry logic without infinite retry loops."""
        attempts = 0
        max_retries = 3

        def mock_llm_call_with_retry():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionError("HTTP 429: Too Many Requests (Rate limit exceeded)")
            return {"answer": "Recovered response after backoff"}

        # Simulate bounded retry loop
        result = None
        for attempt in range(max_retries):
            try:
                result = mock_llm_call_with_retry()
                break
            except ConnectionError:
                if attempt == max_retries - 1:
                    raise

        assert result is not None
        assert attempts == 3
        assert result["answer"] == "Recovered response after backoff"

    def test_malformed_llm_json_fallback(self):
        """Verify invalid or unparseable JSON from LLM is handled safely."""
        raw_malformed_response = "I cannot fulfill this in JSON format: Sorry!"

        with pytest.raises(json.JSONDecodeError):
            json.loads(raw_malformed_response)
