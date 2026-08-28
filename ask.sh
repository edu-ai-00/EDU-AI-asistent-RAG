#!/usr/bin/env bash
# Manual QA tester for the edu_ai search service.
# Usage:  ./ask.sh "your question here"   [session_id]
# Hits the SAME public route the mobile app would use: nginx -> /search/ -> edu_ai_search

Q="${1:?Usage: ./ask.sh \"your question\" [session_id]}"
SID="${2:-manual-$(date +%s)}"
BASE="${SEARCH_BASE:-http://localhost/search}"   # override: SEARCH_BASE=http://localhost:8090 ./ask.sh ...

echo "❓ $Q"
echo "   (session=$SID, endpoint=$BASE/v1/ask)"
echo "────────────────────────────────────────────────────────"

curl -s -m 130 -X POST "$BASE/v1/ask" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"question":sys.argv[1],"session_id":sys.argv[2]}))' "$Q" "$SID")" \
  -w $'\n__HTTP__ %{http_code} __TIME__ %{time_total}\n' \
| python3 -c '
import sys, json, re
raw = sys.stdin.read()
m = re.search(r"__HTTP__ (\d+) __TIME__ ([\d.]+)", raw)
http, t = (m.group(1), m.group(2)) if m else ("?", "?")
body = raw[:m.start()] if m else raw
try:
    d = json.loads(body)
    lang = d.get("user_language")
    strat = d.get("strategies")
    rew = d.get("user_query")
    dur = d.get("duration_ms")
    print("ANSWER:\n" + str(d.get("answer", "(none)")))
    print()
    print("language   : " + str(lang))
    print("strategies : " + str(strat))
    print("rewritten  : " + str(rew))
    srcs = json.loads(d.get("sources", "[]") or "[]")
    print("sources (" + str(len(srcs)) + "):")
    for s in srcs[:8]:
        print("    #" + str(s.get("rank")) + "  " + str(s.get("file_name", "?")))
    print()
    print("server duration_ms: " + str(dur))
except Exception:
    print(body[:1500])
print("HTTP " + http + "   wall " + t + "s")
'
