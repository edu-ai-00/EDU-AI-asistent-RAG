"""Controllable fake search for the school-selection browser test.

Serves /v1/entities, /v1/domains and /v1/ask, records every request, and lets the
test flip its state through /__ctl/*. A question containing "pomalu" is answered
after a delay, so a test can change school while it is in flight.
"""
import asyncio

from fastapi import FastAPI, HTTPException, Request

app = FastAPI()

STATE: dict = {"entities": [], "entities_fail": False, "domains_delay": 0.0, "log": []}


def _row(code: str, name: str) -> dict:
    return {"legal_entity_code": code, "display_name": name, "kind": "legal_person"}


@app.post("/__ctl/set")
async def ctl_set(request: Request):
    body = await request.json()
    if "entities" in body:
        STATE["entities"] = [_row(c, n) for c, n in body["entities"]]
    if "entities_fail" in body:
        STATE["entities_fail"] = bool(body["entities_fail"])
    if "domains_delay" in body:
        STATE["domains_delay"] = float(body["domains_delay"])
    return {"ok": True}


@app.post("/__ctl/reset")
async def ctl_reset():
    STATE.update(entities=[_row("SKOLA_A", "Základní škola Alfa"),
                           _row("SKOLA_B", "Základní škola Beta")],
                 entities_fail=False, domains_delay=0.0, log=[])
    return {"ok": True}


@app.get("/__ctl/log")
async def ctl_log():
    return STATE["log"]


@app.get("/v1/entities")
async def entities():
    STATE["log"].append({"path": "/v1/entities"})
    if STATE["entities_fail"]:
        raise HTTPException(status_code=500, detail="db down")
    return {"entities": STATE["entities"]}


@app.get("/v1/domains")
async def domains(legal_entity: str):
    STATE["log"].append({"path": "/v1/domains", "legal_entity": legal_entity})
    if STATE["domains_delay"]:
        await asyncio.sleep(STATE["domains_delay"])
    if legal_entity not in {r["legal_entity_code"] for r in STATE["entities"]}:
        raise HTTPException(status_code=404, detail="unknown entity")
    return {"legal_entity": legal_entity,
            "domains": [{"code": "ATTENDANCE", "labels": {"cs": "Docházka"}}]}


@app.post("/v1/ask")
async def ask(request: Request):
    body = await request.json()
    STATE["log"].append({"path": "/v1/ask", "legal_entity": body.get("legal_entity"),
                         "conversation_id": body.get("conversation_id")})
    if body.get("legal_entity") not in {r["legal_entity_code"] for r in STATE["entities"]}:
        raise HTTPException(status_code=400, detail="unknown or inactive legal entity")
    if "pomalu" in (body.get("question") or ""):
        await asyncio.sleep(2.0)
    return {"answer": f"Odpověď pro {body['legal_entity']}.", "outcome": "answer",
            "answer_source": "rag", "suggestions": [], "sources": [], "language": "cs",
            "duration_ms": 5, "effective_scope": {"legal_entity": body["legal_entity"]}}
