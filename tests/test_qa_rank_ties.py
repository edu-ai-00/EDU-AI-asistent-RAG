"""Unit tests for the order of the qa_lexical entries (search/app/nodes/qa_lexical.py, _rank_key).

Pure, no DB. Run inside the search image:

    docker exec -i edu_ai_search python - < tests/test_qa_rank_ties.py

What it protects: without `rank_ties` the order is the one before the experiment
(tier, rank, coverage; an exact tie keeps the order the rows came in); with it an
exact tie is settled by the question text and then the id, whatever the input order.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.nodes.qa_lexical import _rank_key  # noqa: E402


def _e(q, rank=0.5, cov=0.6, tier=(0, 0)):
    return {"canonical_q": q, "rank": rank, "cov": cov, "tier": tier}


def test_off_keeps_the_input_order_on_a_tie():
    items = [("b", _e("B")), ("a", _e("A"))]
    assert [k for k, _ in sorted(items, key=lambda kv: _rank_key(kv))] == ["b", "a"]


def test_on_settles_a_tie_by_text_whatever_the_input_order():
    for items in ([("b", _e("B")), ("a", _e("A"))], [("a", _e("A")), ("b", _e("B"))]):
        assert [k for k, _ in sorted(items, key=lambda kv: _rank_key(kv, True))] == ["a", "b"]


def test_same_text_settled_by_id():
    items = [("2", _e("Q")), ("1", _e("Q"))]
    assert [k for k, _ in sorted(items, key=lambda kv: _rank_key(kv, True))] == ["1", "2"]


def test_rank_coverage_and_tier_still_come_first():
    items = [("a", _e("A", rank=0.4)), ("z", _e("Z", rank=0.5)),
             ("y", _e("Y", rank=0.5, cov=0.9)), ("x", _e("X", rank=0.9, tier=(1, 0)))]
    for ties in (False, True):
        assert [k for k, _ in sorted(items, key=lambda kv: _rank_key(kv, ties))] == ["y", "z", "a", "x"]


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"Ran {n} tests")
    print("OK")
