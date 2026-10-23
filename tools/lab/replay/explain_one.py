"""Debug one question in a replay container: the QA steps' decision details."""
import sys, json
import replay as R
import app.server as server
R._patch_models()
from app.executor import run_search
q, entity = sys.argv[1], sys.argv[2]
role = sys.argv[3] if len(sys.argv) > 3 else None
grade = sys.argv[4] if len(sys.argv) > 4 else None
req = server.SearchRequest(question=q, legal_entity=entity, user_role=role, user_school_grade=grade)
st, _ = run_search(server._prepare_search(req)["initial"], stop_before="answer")
print("norm:", st.get("query_norm_tokens"), "| lemma:", st.get("query_lemma_tokens"), "| stem:", st.get("query_stem_tokens"))
for t in st.get("trace") or []:
    if t.get("step") == "qa_lexical":
        d = {k: v for k, v in t.items() if k not in ("step",)}
        for c in (d.get("concepts") or d.get("detail", {}).get("concepts") or []):
            print("  concept", c)
        print("  ", {k: v for k, v in d.items() if k not in ("concepts",)})
print("=>", st.get("answer_source"), st.get("decision"), st.get("match_score"), st.get("match_margin"), st.get("matched_entry_id"))
