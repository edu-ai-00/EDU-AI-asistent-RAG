# last change in code by Martin Safranek (2026-08-01).
"""edu_ai Search Pipeline — deterministic QA→RAG cascade (LangGraph StateGraph).

Replaces the old parallel-RRF graph. Everything is deterministic except the
final `answer` LLM; QA (curated) beats RAG (generated); lexical beats vector;
`qa_vector` is the last curated gate before the generated RAG fallback.

    START → prepare → route ─(blocked/unusable)─────────────────────► END
                        │ ok
                     qa_exact ─(exact hit)──────────────────────────► END
                        │ miss
                     qa_lexical ─(A answer / B suggest)─────────────► END
                        │ C
                     document_lexical ──► qa_vector ─(cosine hit)────────► END
                                        │ miss
                                     rag_vector ──► answer ─────────► END

Each stage sets state["resolved"] when it produces a terminal outcome; the
conditional edges route a resolved state straight to END.
"""

from __future__ import annotations

import logging

from langgraph.graph import StateGraph, START, END

from .state import SearchState
from .nodes.prepare import prepare
from .nodes.route import route
from .nodes.qa_exact import qa_exact
from .nodes.qa_lexical import qa_lexical
from .nodes.qa_vector import qa_vector
from .nodes.document_lexical import document_lexical
from .nodes.rag_vector import rag_vector
from .nodes.answer import answer

logger = logging.getLogger(__name__)


def _gate(next_node: str):
    """Conditional-edge fn: END if the stage resolved, else continue to next_node."""
    def _fn(state: SearchState) -> str:
        return END if state.get("resolved") else next_node
    return _fn


def build_graph():
    g = StateGraph(SearchState)

    g.add_node("prepare", prepare)
    g.add_node("route", route)
    g.add_node("qa_exact", qa_exact)
    g.add_node("qa_lexical", qa_lexical)
    g.add_node("document_lexical", document_lexical)
    g.add_node("qa_vector", qa_vector)
    g.add_node("rag_vector", rag_vector)
    g.add_node("answer", answer)

    g.add_edge(START, "prepare")
    g.add_edge("prepare", "route")

    # guard/exact/lexical each can terminate early
    g.add_conditional_edges("route", _gate("qa_exact"), ["qa_exact", END])
    g.add_conditional_edges("qa_exact", _gate("qa_lexical"), ["qa_lexical", END])
    g.add_conditional_edges("qa_lexical", _gate("document_lexical"), ["document_lexical", END])

    # C fallthrough: gather RAG lexical, then the curated vector gate
    g.add_edge("document_lexical", "qa_vector")
    g.add_conditional_edges("qa_vector", _gate("rag_vector"), ["rag_vector", END])

    # generated last resort
    g.add_edge("rag_vector", "answer")
    g.add_edge("answer", END)

    return g.compile()


app = build_graph()
