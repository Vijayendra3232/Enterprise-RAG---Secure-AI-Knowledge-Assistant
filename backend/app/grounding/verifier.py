import re
import numpy as np
from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from app import config
from app.core import embeddings

# Helper functions for deterministic factual checks

def extract_numbers(text: str) -> set:
    """
    Extracts all digit representations (including simple English word forms)
    from text as a normalized string set.
    """
    word_to_num = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"
    }
    tokens = re.findall(r"\b\w+\b", text.lower())
    numbers = set()
    for t in tokens:
        if t.isdigit():
            numbers.add(t)
        elif t in word_to_num:
            numbers.add(word_to_num[t])
    return numbers

def check_negation_mismatch(claim: str, evidence: str) -> bool:
    """
    Returns True if there is a negation mismatch between the claim and evidence.
    """
    negation_words = ["not", "never", "cannot", "no ", "don't"]
    has_negation_claim = any(w in claim.lower() for w in negation_words)
    has_negation_evidence = any(w in evidence.lower() for w in negation_words)
    return has_negation_claim != has_negation_evidence

def check_modal_mismatch(claim: str, evidence: str) -> bool:
    """
    Returns True if the claim represents a mandatory constraint ('must', 'required', 'mandatory')
    whereas the evidence only describes a preferred/optional recommendation.
    """
    claim_lower = claim.lower()
    evidence_lower = evidence.lower()
    claim_mandatory = any(w in claim_lower for w in ["mandatory", "required", "must", "have to"])
    evidence_optional = any(w in evidence_lower for w in ["preferred", "optional", "recommend", "should"])
    return claim_mandatory and evidence_optional


# Modular Verifier Interface

class VerifierStrategy(ABC):
    @abstractmethod
    def verify(self, claim: str, evidence: str) -> str:
        """
        Verify if the claim is supported by the evidence.
        Returns: 'SUPPORTED', 'PARTIALLY_SUPPORTED', 'UNSUPPORTED'
        """
        pass


class LexicalVerifier(VerifierStrategy):
    """
    Factual verifier checking text overlap and token matching.
    """
    def verify(self, claim: str, evidence: str) -> str:
        claim_words = set(re.findall(r"\b\w{3,}\b", claim.lower()))
        evidence_words = set(re.findall(r"\b\w{3,}\b", evidence.lower()))
        
        if not claim_words:
            return "UNSUPPORTED"
            
        overlap = claim_words.intersection(evidence_words)
        ratio = len(overlap) / len(claim_words)
        
        # Check absolute numeric/negation mismatches first
        if extract_numbers(claim) != extract_numbers(evidence):
            return "UNSUPPORTED"
        if check_negation_mismatch(claim, evidence):
            return "UNSUPPORTED"
        if check_modal_mismatch(claim, evidence):
            return "PARTIALLY_SUPPORTED"

        if ratio >= 0.6:
            return "SUPPORTED"
        elif ratio >= 0.2:
            return "PARTIALLY_SUPPORTED"
        return "UNSUPPORTED"


class SemanticVerifier(VerifierStrategy):
    """
    Verifier combining vector embeddings similarity with strict,
    deterministic overrides for factual assertions (numbers, negations, modalities).
    """
    def __init__(self, min_score: float = config.MIN_GROUNDING_SCORE):
        self.min_score = min_score
        self.embed_model = embeddings.load_embedding_model(config.EMBEDDING_MODEL_NAME)

    def verify(self, claim: str, evidence: str) -> str:
        # ── 1. Strict Factual Checks (Absolute overrides) ──────────────────────
        # Number mismatch: Claims must not invent or alter digits/durations
        claim_nums = extract_numbers(claim)
        evidence_nums = extract_numbers(evidence)
        if claim_nums and not claim_nums.issubset(evidence_nums):
            return "UNSUPPORTED"

        # Negation mismatch: Verify opposing assertions
        if check_negation_mismatch(claim, evidence):
            return "UNSUPPORTED"

        # Modal mismatch: mandatory vs preferred
        is_modal_clash = check_modal_mismatch(claim, evidence)

        # ── 2. Vector Cosine Similarity Check ─────────────────────────────────
        try:
            c_vec = np.array(self.embed_model.embed_query(claim))
            e_vec = np.array(self.embed_model.embed_query(evidence))
            
            dot = np.dot(c_vec, e_vec)
            norm_c = np.linalg.norm(c_vec)
            norm_e = np.linalg.norm(e_vec)
            
            sim = float(dot / (norm_c * norm_e)) if (norm_c > 0 and norm_e > 0) else 0.0
        except Exception as e:
            print(f"[SemanticVerifier] Embedding calculation failed: {e}")
            sim = 0.0

        # Document similarity status logs internally
        # Cosine similarity alone is NOT proof of support.
        if sim < self.min_score:
            return "UNSUPPORTED"

        if is_modal_clash:
            return "PARTIALLY_SUPPORTED"

        return "SUPPORTED"


class GroundingVerifier:
    """
    Coordinates verifier strategy routes to evaluate answer claims.
    """
    def __init__(
        self,
        method: str = config.GROUNDING_METHOD,
        min_score: float = config.MIN_GROUNDING_SCORE
    ):
        self.method = method
        if method == "lexical":
            self.strategy = LexicalVerifier()
        else:
            # Defaults to semantic verifier
            self.strategy = SemanticVerifier(min_score=min_score)

    def verify_claim(self, claim: str, evidence: str) -> str:
        """
        Public wrapper to verify a single claim sentence against evidence.
        """
        return self.strategy.verify(claim, evidence)
