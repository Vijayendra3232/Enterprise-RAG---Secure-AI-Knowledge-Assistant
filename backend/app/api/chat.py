"""
chat.py — Protected Chat-Assistant API endpoints with tenant-isolated caching and authorization context.
"""

import time
import threading
from typing import Optional
from fastapi import APIRouter, HTTPException, Request, Depends, status
from pydantic import BaseModel

from app import config
from app.auth.models import User
from app.auth.dependencies import get_current_user, get_current_auth_context
from app.authorization.policy import require_permission
from app.authorization.permissions import Permission
from app.authorization.context import AuthorizationContext
from app.security.audit import log_security_event

router = APIRouter()

# Cache format: { f"{tenant_id}_{user_id}:{question}": (answer, docs, last_invocation_time) }
cache = {}
CACHE_TIMEOUT = config.CACHE_TIMEOUT
cache_lock = threading.Lock()


def monitor_cache_timeout():
    """
    Monitor cache timeout and remove entries older than CACHE_TIMEOUT
    """
    while True:
        current_time = time.time()
        with cache_lock:
            for cache_key, data in list(cache.items()):
                _, _, last_time = data
                if current_time - last_time > CACHE_TIMEOUT:
                    print(f"[CACHE] Entry expired: {cache_key}")
                    cache.pop(cache_key, None)
        time.sleep(60)


# Start the timer monitor thread
timer_thread = threading.Thread(target=monitor_cache_timeout)
timer_thread.daemon = True
timer_thread.start()


class ChatRequest(BaseModel):
    question: str


@router.get("/Chat-Assistant/")
async def chat_assistant_get(
    question: str,
    request: Request,
    current_user: User = Depends(require_permission(Permission.DOCUMENT_READ)),
    auth_context: AuthorizationContext = Depends(get_current_auth_context)
):
    """
    GET /Chat-Assistant/ — Query the enterprise RAG assistant with strict authorization scoping.
    """
    return await _process_chat(question, request, current_user, auth_context)


@router.post("/Chat-Assistant/")
async def chat_assistant_post(
    chat_req: ChatRequest,
    request: Request,
    current_user: User = Depends(require_permission(Permission.DOCUMENT_READ)),
    auth_context: AuthorizationContext = Depends(get_current_auth_context)
):
    """
    POST /Chat-Assistant/ — JSON body alternative for chat queries.
    """
    return await _process_chat(chat_req.question, request, current_user, auth_context)


async def _process_chat(
    question: str,
    request: Request,
    current_user: User,
    auth_context: AuthorizationContext
):
    if not question or not question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    current_time = time.time()
    # Cache key incorporates tenant_id and user_id to ensure zero cross-user/tenant data leakage
    cache_key = f"{current_user.tenant_id}_{current_user.user_id}:{question.strip()}"

    # ------------------ CACHE HIT -------------------
    with cache_lock:
        if cache_key in cache:
            answer, docs, _ = cache[cache_key]
            # update last access time
            cache[cache_key] = (answer, docs, current_time)
            print(f"[CACHE] Hit for user {current_user.user_id}:", question)
            return {"response": answer, "docs": docs}

    # ------------------ CACHE MISS ------------------
    print(f"[CACHE] Miss for user {current_user.user_id}:", question)

    try:
        rag_service = getattr(request.app.state, "rag_service", None)
        if not rag_service:
            raise HTTPException(status_code=500, detail="RAG service is not initialized.")

        answer, docs = rag_service.answer_question(question, auth_context=auth_context)

        # Save into cache
        with cache_lock:
            cache[cache_key] = (answer, docs, current_time)
            MAX_CACHE_SIZE = config.MAX_CACHE_SIZE
            if len(cache) > MAX_CACHE_SIZE:
                cache.pop(next(iter(cache)))

        # Log security audit event for chat request
        log_security_event(
            event_type="CHAT_REQUEST",
            tenant_id=current_user.tenant_id,
            user_id=current_user.user_id,
            action="query",
            result="success",
            details={
                "question_length": len(question),
                "num_citations": len(docs)
            }
        )

        return {"response": answer, "docs": docs}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
