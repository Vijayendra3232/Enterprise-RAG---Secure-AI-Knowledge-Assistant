from typing import Dict, Any, List, Tuple, Optional
import os
import time
import re

from app import config
from app.core.prompts import Prompt, SYSTEM_PROMPT, USER_PROMPT, prompt_struct
from app.retrieval.vector import VectorStoreInterface
from app.retrieval.pipeline import RetrievalPipeline

# Step 4 imports
from app.query.analyzer import QueryAnalyzer
from app.query.rewriter import QueryRewriter
from app.query.decomposer import QueryDecomposer
from app.context.deduplicator import ContextDeduplicator
from app.context.compressor import ContextCompressor
from app.context.organizer import ContextOrganizer

# Step 5 imports
from app.generation.generator import GroundedGenerator
from app.grounding.citation import CitationBuilder, CitationValidator
from app.grounding.claims import ClaimExtractor
from app.grounding.verifier import GroundingVerifier
from app.generation.models import AnswerResponse, CitationResponse, GroundingResponse


from app.authorization.context import AuthorizationContext
from app.authorization.filters import is_document_accessible


class RAGService:
    """
    RAGService coordinates Query Intelligence, Advanced Hybrid Retrieval,
    Context Optimization, Grounded LLM Generation, Claim Extraction, and Factual Grounding Verification.
    Enforces deterministic multi-tenant and document access control.
    """
    def __init__(
        self,
        vector_db: Optional[Any] = None,
        search_store: Optional[Any] = None,
        llm_model_id: str = config.GROQ_MODEL_ID,
    ):
        self.vector_db = vector_db
        self.search_store = search_store or (vector_db if hasattr(vector_db, "vector_search") else None)
        
        # Retrieval Pipeline
        self.retrieval_pipeline = RetrievalPipeline(
            vector_store=self.vector_db,
            search_store=self.search_store,
        )

        
        # Query Intelligence Layers
        self.query_analyzer = QueryAnalyzer()
        self.query_rewriter = QueryRewriter()
        self.query_decomposer = QueryDecomposer()
        
        # Context Optimization Layers
        self.context_deduplicator = ContextDeduplicator()
        self.context_compressor = ContextCompressor()
        self.context_organizer = ContextOrganizer()

        # Grounding & Generation Layers
        self.grounded_generator = GroundedGenerator(llm_model_id)
        self.claim_extractor = ClaimExtractor()

    def answer_question(
        self,
        question: str,
        auth_context: Optional[AuthorizationContext] = None
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Backward-compatible API entry point returning (answer_text, citations_list).
        Delegates internally to answer_question_grounded.
        """
        response_obj = self.answer_question_grounded(question, auth_context=auth_context)
        
        # Map CitationResponse objects back to dict list contract expected by existing endpoint
        citations_mapped = []
        for c in response_obj.citations:
            citations_mapped.append({
                "chunk_id": c.chunk_id,
                "document_id": c.document_id,
                "filename": c.filename,
                "source": c.source,
                "page": c.page
            })
            
        return response_obj.response, citations_mapped

    def answer_question_grounded(
        self,
        question: str,
        auth_context: Optional[AuthorizationContext] = None
    ) -> AnswerResponse:
        """
        Main grounded generation flow executing advanced query classification, search,
        context budget allocation, LLM drafting, claim verification, and citation validation.
        Enforces strict authorization boundaries at every step.
        """
        from app.observability.tracing import tracer
        from app.observability.metrics import (
            query_analysis_total,
            query_rewrite_total,
            query_decomposition_total,
            query_intelligence_duration_seconds,
            context_optimization_total,
            context_optimization_duration_seconds,
            grounding_checks_total,
            grounding_duration_seconds,
            citation_validation_failures_total,
        )
        from app.observability.schemas import (
            QueryTelemetry,
            ContextTelemetry,
            GroundingTelemetry,
        )
        from app.observability.timing import StageTimer

        timer = StageTimer()

        # ── 1. Query Analysis Stage ──────────────────────────────────────────
        timer.start_stage("query_analysis")
        with tracer.start_span("rag.query_analysis") as span:
            try:
                analysis = self.query_analyzer.analyze(question)
                query_analysis_total.inc(labels={"status": "success"})
            except Exception as e:
                query_analysis_total.inc(labels={"status": "error"})
                if span:
                    span.set_status("ERROR")
                print(f"[RAGService] Query analyzer failed: {e}")
                from app.query.models import QueryAnalysis
                analysis = QueryAnalysis(original_query=question)
        analysis_latency = timer.stop_stage("query_analysis")
        query_intelligence_duration_seconds.observe(analysis_latency / 1000.0, labels={"stage": "analysis"})

        # ── 2. Query Rewriting Stage ──────────────────────────────────────────
        timer.start_stage("query_rewrite")
        with tracer.start_span("rag.query_rewrite") as span:
            try:
                search_query = self.query_rewriter.rewrite(analysis)
                query_rewrite_total.inc(labels={"status": "success"})
            except Exception as e:
                query_rewrite_total.inc(labels={"status": "error"})
                if span:
                    span.set_status("ERROR")
                print(f"[RAGService] Query rewriter failed: {e}")
                search_query = question
        timer.stop_stage("query_rewrite")

        # ── 3. Query Decomposition Stage ──────────────────────────────────────
        timer.start_stage("query_decomposition")
        with tracer.start_span("rag.query_decomposition") as span:
            try:
                sub_queries = self.query_decomposer.decompose(analysis)
                query_decomposition_total.inc(labels={"status": "success"})
            except Exception as e:
                query_decomposition_total.inc(labels={"status": "error"})
                if span:
                    span.set_status("ERROR")
                print(f"[RAGService] Query decomposer failed: {e}")
                sub_queries = [search_query]
        timer.stop_stage("query_decomposition")

        # ── 4. Retrieval Stage (Searches Sub-queries with Auth Constraints) ──
        timer.start_stage("retrieval")
        raw_candidates = []
        with tracer.start_span("rag.retrieval", attributes={"sub_query_count": len(sub_queries)}) as span:
            for sq in sub_queries:
                try:
                    response = self.retrieval_pipeline.search(
                        sq,
                        filters=None,
                        auth_context=auth_context,
                        rerank=False
                    )
                    raw_candidates.extend(response.results)
                except Exception as e:
                    if span:
                        span.set_status("ERROR")
                    print(f"[RAGService] Retrieval failed for sub-query: {e}")
        retrieval_latency = timer.stop_stage("retrieval")

        # ── 5. Context Optimization Stage ─────────────────────────────────────
        timer.start_stage("context_optimization")
        with tracer.start_span("rag.context_optimization") as span:
            try:
                deduped = self.context_deduplicator.deduplicate(raw_candidates)
                compressed = self.context_compressor.compress(question, deduped)
                
                # Final Reranking (Uses ORIGINAL user question)
                with tracer.start_span("rag.reranking"):
                    reranked = self.retrieval_pipeline.reranker.rerank(question, compressed)
                
                # Context Budget Enforcer (Atomically budgets chunks)
                final_candidates = self.context_organizer.organize(reranked)
                context_optimization_total.inc(labels={"status": "success"})
            except Exception as e:
                context_optimization_total.inc(labels={"status": "error"})
                if span:
                    span.set_status("ERROR")
                print(f"[RAGService] Context optimization failed: {e}")
                final_candidates = raw_candidates[:config.MAX_CONTEXT_CHUNKS]
        context_opt_latency = timer.stop_stage("context_optimization")
        context_optimization_duration_seconds.observe(context_opt_latency / 1000.0, labels={"operation": "pipeline"})

        # ── Strict Final Authorization Check (Defense-in-depth) ───────────────
        if auth_context is not None:
            final_candidates = [
                c for c in final_candidates
                if is_document_accessible(c.metadata, auth_context)
            ]

        # ── 6. Citation Builder Stage (System-owned mapping) ─────────────────
        citation_builder = CitationBuilder()
        system_citations, citation_mapping = citation_builder.build_citations(final_candidates)

        # Handle Insufficient Evidence Refusal early
        refusal_msg = "I couldn't find enough information in the available documents to answer that reliably."
        if not final_candidates:
            grounding_checks_total.inc(labels={"status": "INSUFFICIENT_EVIDENCE"})
            return AnswerResponse(
                response=refusal_msg,
                citations=[],
                grounding=GroundingResponse(grounded=False, status="INSUFFICIENT_EVIDENCE"),
                diagnostics={
                    "latency_ms": timer.elapsed_ms,
                    "analysis_latency_ms": analysis_latency,
                    "retrieval_latency_ms": retrieval_latency,
                    "total_latency_ms": timer.elapsed_ms,
                }
            )

        # ── 7. LLM Generation Stage (JSON output schema) ─────────────────────
        timer.start_stage("generation")
        context_texts = [c.content for c in final_candidates]
        with tracer.start_span("rag.llm_generation", attributes={"context_chunk_count": len(context_texts)}) as span:
            try:
                draft_answer, structured_claims, refusal_triggered = self.grounded_generator.generate(
                    question, context_texts
                )
            except Exception as e:
                if span:
                    span.set_status("ERROR")
                print(f"[RAGService] LLM Generation failed: {e}")
                draft_answer = refusal_msg
                structured_claims = []
                refusal_triggered = True
        gen_latency = timer.stop_stage("generation")

        if refusal_triggered:
            grounding_checks_total.inc(labels={"status": "INSUFFICIENT_EVIDENCE"})
            return AnswerResponse(
                response=refusal_msg,
                citations=[],
                grounding=GroundingResponse(grounded=False, status="INSUFFICIENT_EVIDENCE"),
                diagnostics={
                    "latency_ms": timer.elapsed_ms,
                    "analysis_latency_ms": analysis_latency,
                    "retrieval_latency_ms": retrieval_latency,
                    "generation_latency_ms": gen_latency,
                    "total_latency_ms": timer.elapsed_ms,
                }
            )

        # ── 8. Claim Extraction Stage ─────────────────────────────────────────
        timer.start_stage("claim_extraction")
        with tracer.start_span("rag.claim_extraction"):
            try:
                claims = self.claim_extractor.extract_claims(draft_answer, structured_claims)
            except Exception as e:
                print(f"[RAGService] Claim extraction failed: {e}")
                claims = []
        timer.stop_stage("claim_extraction")

        # ── 9. Citation Validation Stage ──────────────────────────────────────
        validator = CitationValidator(citation_mapping)
        cleaned_draft, _ = validator.validate_citations(draft_answer, [])
        for c in claims:
            # Check that referenced evidence IDs exist
            c.evidence_ids = [eid for eid in c.evidence_ids if eid in citation_mapping]

        # ── 10. Grounding Verification Stage ──────────────────────────────────
        timer.start_stage("grounding")
        verifier = GroundingVerifier()
        supported_claims = []
        partially_supported_claims = []
        unsupported_claims = []

        with tracer.start_span("rag.grounding_verification", attributes={"claim_count": len(claims)}):
            for claim in claims:
                # If claim references no evidence, find if any chunk supports it
                if not claim.evidence_ids:
                    best_status = "UNSUPPORTED"
                    for cid, chunk in citation_mapping.items():
                        status = verifier.verify_claim(claim.text, chunk.content)
                        if status == "SUPPORTED":
                            best_status = "SUPPORTED"
                            claim.evidence_ids.append(cid)
                            break
                        elif status == "PARTIALLY_SUPPORTED":
                            best_status = "PARTIALLY_SUPPORTED"
                            claim.evidence_ids.append(cid)
                    status = best_status
                else:
                    # Verify claim against cited evidence chunks
                    statuses = []
                    for eid in claim.evidence_ids:
                        chunk = citation_mapping[eid]
                        statuses.append(verifier.verify_claim(claim.text, chunk.content))
                    
                    if "UNSUPPORTED" in statuses:
                        status = "UNSUPPORTED"
                    elif "PARTIALLY_SUPPORTED" in statuses:
                        status = "PARTIALLY_SUPPORTED"
                    else:
                        status = "SUPPORTED"

                if status == "SUPPORTED":
                    supported_claims.append(claim)
                elif status == "PARTIALLY_SUPPORTED":
                    partially_supported_claims.append(claim)
                else:
                    unsupported_claims.append(claim)

        grounding_latency = timer.stop_stage("grounding")

        # ── 11. Enforce Answer Policy ─────────────────────────────────────────
        final_answer = cleaned_draft
        grounded_status = "SUPPORTED"
        grounded_bool = True

        if unsupported_claims:
            if config.REFUSE_ON_UNSUPPORTED_CLAIMS:
                # Filter out sentences containing unsupported claims
                sentences = [s.strip() for s in re.split(r"(?<=\.|\?)\s+", cleaned_draft) if s.strip()]
                final_sentences = []
                for s in sentences:
                    # Clean tags for matching text
                    clean_s = re.sub(r"\[C\d+\]", "", s).strip()
                    clean_s = re.sub(r"\s+", " ", clean_s)
                    
                    is_unsupported = False
                    for uc in unsupported_claims:
                        # Clean punctuation for exact/subset matching
                        sent_clean_norm = re.sub(r"[^\w\s]", "", clean_s.lower()).strip()
                        uc_clean_norm = re.sub(r"[^\w\s]", "", uc.text.lower()).strip()
                        
                        words_sent = set(sent_clean_norm.split())
                        words_uc = set(uc_clean_norm.split())
                        
                        overlap_ratio = len(words_sent.intersection(words_uc)) / max(1, len(words_uc))
                        if uc_clean_norm in sent_clean_norm or sent_clean_norm in uc_clean_norm or overlap_ratio > 0.85:
                            is_unsupported = True
                            break
                    if not is_unsupported:
                        final_sentences.append(s)
                
                final_answer = " ".join(final_sentences).strip()
                grounded_status = "PARTIALLY_SUPPORTED"
                
                # If everything is filtered out or answer is empty, refuse
                if not final_answer or len(final_answer) < 15:
                    final_answer = refusal_msg
                    grounded_status = "INSUFFICIENT_EVIDENCE"
                    grounded_bool = False
            else:
                grounded_status = "UNSUPPORTED"
                grounded_bool = False

        if partially_supported_claims and grounded_status == "SUPPORTED":
            grounded_status = "PARTIALLY_SUPPORTED"

        # Record grounding metrics
        grounding_checks_total.inc(labels={"status": grounded_status})
        grounding_duration_seconds.observe(grounding_latency / 1000.0, labels={"status": grounded_status})

        # Final Citations Filter: Only show citations that verified at least one claim
        active_cids = set()
        if final_answer != refusal_msg:
            # Collect all active citation tags present in the final answer
            found_tags = re.findall(r"\[(C\d+)\]", final_answer)
            active_cids.update(found_tags)
            
            # Also collect evidence_ids from supported/partially supported claims
            for sc in supported_claims + partially_supported_claims:
                active_cids.update(sc.evidence_ids)

        verified_citations = [c for c in system_citations if c.citation_id in active_cids]

        total_latency = timer.elapsed_ms
        
        diagnostics = {
            "analysis_latency_ms": analysis_latency,
            "retrieval_latency_ms": retrieval_latency,
            "generation_latency_ms": gen_latency,
            "grounding_latency_ms": grounding_latency,
            "total_latency_ms": total_latency,
            "claims_count": len(claims),
            "supported_claims_count": len(supported_claims),
            "unsupported_claims_count": len(unsupported_claims),
            "partially_supported_claims_count": len(partially_supported_claims),
            "refused": final_answer == refusal_msg
        }

        return AnswerResponse(
            response=final_answer,
            citations=verified_citations,
            grounding=GroundingResponse(grounded=grounded_bool, status=grounded_status),
            diagnostics=diagnostics
        )

