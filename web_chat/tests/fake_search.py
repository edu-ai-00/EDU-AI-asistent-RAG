# last change in code by Jan Drozd and Martin Safranek (2026-08-13).
"""Deterministic local upstream used only for browser smoke testing."""

import json

from fastapi import FastAPI
from pydantic import BaseModel


app = FastAPI()


class SearchRequest(BaseModel):
    question: str
    session_id: str | None = None
    conversation_id: str | None = None
    user_id: str | None = None
    user_school_grade: int | None = None


@app.post("/v1/ask")
async def search(payload: SearchRequest):
    if "návrh" in payload.question.lower():
        return {
            "answer": "Vyberte jednu z podobných otázek:",
            "outcome": "suggest",
            "answer_source": "qa_lexical",
            "suggestions": [
                {"entry_id": "demo-1", "question": "Jak podat přihlášku na střední školu?"},
                {"entry_id": "demo-2", "question": "Kdy se podává přihláška?"},
            ],
            "sources": "[]",
            "language": "cs",
            "duration_ms": 18,
        }

    return {
        "answer": "Toto je **zkušební odpověď** z lokálního API.\n\n- První bod\n- Druhý bod",
        "outcome": "answer",
        "answer_source": "rag",
        "suggestions": [],
        "sources": json.dumps(
            [{"file_name": "testovaci-dokument.pdf", "file_uuid": "internal-id"}],
            ensure_ascii=False,
        ),
        "language": "cs",
        "duration_ms": 27,
    }
