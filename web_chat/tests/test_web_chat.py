# last change in code by Jan Drozd and Martin Safranek (2026-08-18).
import uuid

import httpx
from fastapi.testclient import TestClient

from web_chat import app, get_search_client, map_sources


class FakeSearchClient:
    """A stand-in for the httpx client, now for GET as well as POST.

    GET arrived with /chat/api/domains: the catalog is fetched from search like
    everything else, so a test of the picker's endpoint has to be able to answer
    one. `get_response` is separate from `response` because a single test
    exercises both verbs (send a message, then read the catalog) and one slot
    would make them share an answer.

    `last_json` / `last_params` are what the assertions about FORWARDING read —
    the interesting question for a BFF is usually not what it returned but what
    it sent on.
    """

    def __init__(self, response: httpx.Response | None = None,
                 error: Exception | None = None,
                 get_response: httpx.Response | None = None,
                 get_error: Exception | None = None):
        self.response = response
        self.error = error
        self.get_response = get_response
        self.get_error = get_error
        self.last_json = None
        self.last_params = None

    async def post(self, _url: str, json: dict):
        self.last_json = json
        if self.error:
            raise self.error
        return self.response

    async def get(self, _url: str, params: dict | None = None):
        self.last_params = params
        if self.get_error:
            raise self.get_error
        return self.get_response


def response(payload: dict, status_code: int = 200) -> httpx.Response:
    request = httpx.Request("POST", "http://search/search")
    return httpx.Response(status_code, json=payload, request=request)


def get_response(payload: dict, status_code: int = 200) -> httpx.Response:
    request = httpx.Request("GET", "http://search/v1/domains")
    return httpx.Response(status_code, json=payload, request=request)


CATALOG = {
    "legal_entity": "SKOLA_TEST",
    "domains": [
        {"code": "ATTENDANCE", "labels": {"cs": "Docházka", "en": "Attendance"}},
        {"code": "CATERING", "labels": {"cs": "Stravování", "en": "Catering"}},
    ],
}


def test_message_maps_current_search_contract():
    fake = FakeSearchClient(
        response(
            {
                "answer": "Výsledná **odpověď**.",
                "outcome": "suggest",
                "answer_source": "qa_lexical",
                "suggestions": [{"entry_id": "one", "question": "Kanonická otázka?"}],
                "sources": '[{"file_name":"pravidla.pdf","file_uuid":"private"}]',
                "language": "cs",
                "duration_ms": 42,
            }
        )
    )
    app.dependency_overrides[get_search_client] = lambda: fake

    conversation_id = str(uuid.uuid4())
    with TestClient(app) as client:
        result = client.post(
            "/chat/api/message",
            json={
                "message": "  Jak to funguje?  ",
                "school_grade": 9,
                "user_role": "ucitel",
                "conversation_id": conversation_id,
            },
        )

    app.dependency_overrides.clear()
    assert result.status_code == 200
    assert result.json()["suggestions"] == [
        {"kind": "qa", "entry_id": "one", "question": "Kanonická otázka?", "options": []}
    ]
    assert result.json()["sources"] == [{"title": "pravidla.pdf", "page": None, "url": None}]
    assert fake.last_json["question"] == "Jak to funguje?"
    assert fake.last_json["user_school_grade"] == 9
    # translated, NOT passed through: /search validates the role against
    # code_list('role') and 'ucitel' is not in it. This assertion used to expect
    # the slug, which is why the drift reached production.
    assert fake.last_json["user_role"] == "TEACHER"
    assert fake.last_json["conversation_id"] == conversation_id
    assert result.cookies.get("edu_ai_chat_session")


def test_sources_arrive_as_an_array():
    # /search returns a real array since 2026-08-13. map_sources keeps tolerating the
    # old JSON string (test above) because this hop crosses a network to a service
    # that may still be the previous build.
    fake = FakeSearchClient(
        response(
            {
                "answer": "Odpověď.",
                "outcome": "answer",
                "answer_source": "rag",
                "sources": [{"file_name": "pravidla.pdf", "page": 12}],
            }
        )
    )
    app.dependency_overrides[get_search_client] = lambda: fake

    with TestClient(app) as client:
        result = client.post("/chat/api/message", json={"message": "Otázka"})

    app.dependency_overrides.clear()
    assert result.json()["sources"] == [{"title": "pravidla.pdf", "page": 12, "url": None}]


def test_llm_unavailable_is_not_coerced_to_no_answer():
    # same trap role_required fell into: an outcome the BFF's Literal does not
    # know is rewritten to no_answer, and the UI then prints "Odpověď nebyla
    # nalezena" over an outage — the exact confusion init 139 removes.
    fake = FakeSearchClient(
        response(
            {
                "answer": "Teď nemůžu odpověď připravit kvůli dočasnému technickému problému.",
                "outcome": "llm_unavailable",
                "answer_source": "none",
                "language": "cs",
            }
        )
    )
    app.dependency_overrides[get_search_client] = lambda: fake

    with TestClient(app) as client:
        result = client.post("/chat/api/message", json={"message": "Otázka"})

    app.dependency_overrides.clear()
    assert result.json()["outcome"] == "llm_unavailable"


def test_clarify_keeps_its_options():
    # clarify's own question is the ask, not something to send. The options carry
    # the sendable text — dropping them left the UI with one button that asked the
    # question back and always landed in no_answer.
    fake = FakeSearchClient(
        response(
            {
                "answer": "",
                "outcome": "suggest",
                "answer_source": "none",
                "suggestions": [
                    {
                        "kind": "clarify",
                        "question": "Upřesni prosím, kterou oblast myslíš:",
                        "options": [
                            {"label": "Informace školy", "question": "ReRekord - recykluj mobil"},
                            {"label": "Strategický dokument", "question": "Zákaz mobilů ve školách"},
                        ],
                    }
                ],
            }
        )
    )
    app.dependency_overrides[get_search_client] = lambda: fake

    with TestClient(app) as client:
        result = client.post("/chat/api/message", json={"message": "Co dělat s mobily?"})

    app.dependency_overrides.clear()
    assert result.json()["suggestions"] == [
        {
            "kind": "clarify",
            "entry_id": "",
            "question": "Upřesni prosím, kterou oblast myslíš:",
            "options": [
                {"label": "Informace školy", "question": "ReRekord - recykluj mobil"},
                {"label": "Strategický dokument", "question": "Zákaz mobilů ve školách"},
            ],
        }
    ]


def test_clarify_without_options_is_dropped():
    # nothing to render: the prompt cannot be sent and there are no buttons to
    # build from. Better no suggestion than a button that dead-ends.
    fake = FakeSearchClient(
        response(
            {
                "answer": "",
                "outcome": "suggest",
                "answer_source": "none",
                "suggestions": [
                    {"kind": "clarify", "question": "Upřesni prosím:"},
                    {"kind": "variant", "question": "Přeformulovaná otázka?"},
                ],
            }
        )
    )
    app.dependency_overrides[get_search_client] = lambda: fake

    with TestClient(app) as client:
        result = client.post("/chat/api/message", json={"message": "Otázka"})

    app.dependency_overrides.clear()
    # variant survives untouched: an unknown-to-the-frontend kind is still a
    # plain send-the-question button.
    assert result.json()["suggestions"] == [
        {"kind": "variant", "entry_id": "", "question": "Přeformulovaná otázka?", "options": []}
    ]


def test_role_required_is_not_coerced_to_no_answer():
    # /search answers role_required with a 200 carrying the prompt. Coercing it made
    # the UI print "Odpověď nebyla nalezena" above "vyberte roli".
    fake = FakeSearchClient(
        response(
            {
                "answer": "Neznáme tvoji roli.",
                "outcome": "role_required",
                "answer_source": "guard",
                "language": "cs",
            }
        )
    )
    app.dependency_overrides[get_search_client] = lambda: fake

    with TestClient(app) as client:
        result = client.post("/chat/api/message", json={"message": "Otázka"})

    app.dependency_overrides.clear()
    assert result.status_code == 200
    assert result.json()["outcome"] == "role_required"


def test_unknown_outcome_still_falls_back():
    fake = FakeSearchClient(
        response({"answer": "…", "outcome": "rag_telepathy", "answer_source": "none"})
    )
    app.dependency_overrides[get_search_client] = lambda: fake

    with TestClient(app) as client:
        result = client.post("/chat/api/message", json={"message": "Otázka"})

    app.dependency_overrides.clear()
    assert result.json()["outcome"] == "no_answer"


def test_timeout_has_stable_error_shape():
    request = httpx.Request("POST", "http://search/search")
    fake = FakeSearchClient(error=httpx.ReadTimeout("too slow", request=request))
    app.dependency_overrides[get_search_client] = lambda: fake

    with TestClient(app) as client:
        result = client.post("/chat/api/message", json={"message": "Otázka"})

    app.dependency_overrides.clear()
    assert result.status_code == 504
    assert result.json()["error"]["code"] == "search_timeout"


def test_invalid_message_has_stable_error_shape():
    """422 with `fields`, not the old 400 with one sentence (2026-09-06).

    A DELIBERATE CONTRACT CHANGE, made when the area filter added a second field
    a user can get wrong. The old handler answered every rejected field with 400
    and the sentence "Zkontrolujte zadanou otázku a ročník" — which sends someone
    to fix their question when what failed was their area selection. It is now
    the same status and the same envelope search uses for the same class of
    problem, so a client switches on one status instead of two, and `fields`
    names what was actually rejected.

    The frontend renders error.message and is unaffected; the code changed from
    `invalid_message` to `invalid_request` because it is no longer only about
    the message.
    """
    with TestClient(app) as client:
        result = client.post("/chat/api/message", json={"message": "   "})

    assert result.status_code == 422
    body = result.json()["error"]
    assert body["code"] == "invalid_request"
    assert body["retryable"] is False
    # the rejected field is NAMED — that is the whole point of the new shape
    assert any(f["field"] == "message" for f in body["fields"]), body["fields"]


# ══════════════════════════════════════════════════════════════════════
#  AREA SCOPE (spec.search-domain-scope) — the BFF's four jobs:
#  serve the catalog, forward the choice, pass effective_scope back, and
#  turn an upstream 422/503 into something the user can act on.
# ══════════════════════════════════════════════════════════════════════
def test_domains_catalog_is_served_with_the_entity_it_belongs_to():
    fake = FakeSearchClient(get_response=get_response(CATALOG))
    app.dependency_overrides[get_search_client] = lambda: fake

    with TestClient(app) as client:
        result = client.get("/chat/api/domains")

    app.dependency_overrides.clear()
    assert result.status_code == 200
    body = result.json()
    # The ENTITY, echoed. Without it a browser cannot tell a catalog it asked
    # for from the GLOBAL fallback it got, and a picker bound to the wrong
    # school offers codes that then 422.
    assert body["legal_entity"] == "SKOLA_TEST"
    assert [d["code"] for d in body["domains"]] == ["ATTENDANCE", "CATERING"]
    # LABELS travel, so the frontend needs no copy of the catalog
    assert body["domains"][0]["labels"]["cs"] == "Docházka"
    assert "entity_picker" in body


def test_domains_catalog_failure_is_a_retryable_503_not_an_empty_list():
    """An empty list would render as "this school has no areas" and silently
    remove the filter — the reset spec §7 forbids. So: an error with a retry."""
    request = httpx.Request("GET", "http://search/v1/domains")
    fake = FakeSearchClient(get_error=httpx.ReadTimeout("slow", request=request))
    app.dependency_overrides[get_search_client] = lambda: fake

    with TestClient(app) as client:
        result = client.get("/chat/api/domains")

    app.dependency_overrides.clear()
    assert result.status_code == 503
    body = result.json()["error"]
    assert body["code"] == "domains_unavailable"
    assert body["retryable"] is True


def test_domains_are_forwarded_and_effective_scope_comes_back():
    fake = FakeSearchClient(
        response(
            {
                "answer": "Odpověď.",
                "outcome": "answer",
                "answer_source": "qa_exact",
                "suggestions": [],
                "sources": [],
                "language": "cs",
                "duration_ms": 5,
                "effective_scope": {
                    "legal_entity": "SKOLA_TEST",
                    "role": "PARENT",
                    "school_grade": None,
                    "language": None,
                    "domains": ["ATTENDANCE"],
                    # internal, and must NOT reach the browser
                    "identified": False,
                },
            }
        )
    )
    app.dependency_overrides[get_search_client] = lambda: fake

    with TestClient(app) as client:
        result = client.post("/chat/api/message",
                             json={"message": "Otázka?", "domains": ["ATTENDANCE"]})

    app.dependency_overrides.clear()
    assert result.status_code == 200
    # FORWARDED, verbatim — search is the authority for canonicalisation
    assert fake.last_json["domains"] == ["ATTENDANCE"]
    scope = result.json()["effective_scope"]
    assert scope["domains"] == ["ATTENDANCE"]
    # only the five fields the UI needs; `identified` stays on the server side
    assert set(scope) == {"legal_entity", "role", "school_grade", "language", "domains"}


def test_no_domains_means_the_field_is_OMITTED_upstream():
    """Not sent as null. An unrestricted body then stays byte-identical to the
    one this service sent before the field existed, which is what keeps an
    Idempotency-Key digest taken by the old code replaying instead of 409-ing."""
    fake = FakeSearchClient(
        response({"answer": "A", "outcome": "answer", "answer_source": "rag",
                  "suggestions": [], "sources": [], "language": "cs",
                  "duration_ms": 1})
    )
    app.dependency_overrides[get_search_client] = lambda: fake

    with TestClient(app) as client:
        result = client.post("/chat/api/message", json={"message": "Otázka?"})

    app.dependency_overrides.clear()
    assert result.status_code == 200
    assert "domains" not in fake.last_json, fake.last_json
    # an older upstream sends no effective_scope, and that must not be an error
    assert result.json()["effective_scope"] is None


def test_upstream_422_becomes_a_fixable_422_with_fields():
    """It used to be flattened into a 500 saying "chyba na naší straně,
    opakování nepomůže" — about a choice the user can fix in one click."""
    fake = FakeSearchClient(
        response(
            {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "domains: unknown or inactive domain code(s) 'GONE'",
                    "retryable": False,
                    "fields": [{"field": "domains[0]",
                                "message": "unknown or inactive domain code 'GONE'"}],
                }
            },
            status_code=422,
        )
    )
    app.dependency_overrides[get_search_client] = lambda: fake

    with TestClient(app) as client:
        result = client.post("/chat/api/message",
                             json={"message": "Otázka?", "domains": ["GONE"]})

    app.dependency_overrides.clear()
    assert result.status_code == 422
    body = result.json()["error"]
    assert body["code"] == "invalid_scope"
    assert body["retryable"] is False
    assert body["fields"] == [{"field": "domains[0]",
                               "message": "unknown or inactive domain code 'GONE'"}]


def test_upstream_503_is_retryable_and_is_not_retried_without_the_filter():
    """The catalog could not be read, so the NARROWER search the user asked for
    could not run. A wider answer is a different answer, so the BFF reports and
    does not retry — asserted by counting the upstream calls."""
    calls = {"n": 0}

    class CountingClient(FakeSearchClient):
        async def post(self, url, json):
            calls["n"] += 1
            return await super().post(url, json)

    fake = CountingClient(
        response({"error": {"code": "UNAVAILABLE", "message": "catalog down",
                            "retryable": True}}, status_code=503)
    )
    app.dependency_overrides[get_search_client] = lambda: fake

    with TestClient(app) as client:
        result = client.post("/chat/api/message",
                             json={"message": "Otázka?", "domains": ["ATTENDANCE"]})

    app.dependency_overrides.clear()
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "scope_unavailable"
    assert result.json()["error"]["retryable"] is True
    assert calls["n"] == 1, "the BFF must not retry with a wider scope"


def test_the_bff_refuses_the_shapes_search_refuses():
    """Rejected HERE, so a body search will certainly refuse is not forwarded —
    and rejected in the same fixable shape."""
    with TestClient(app) as client:
        for payload, why in (
            ({"message": "Q", "domains": []}, "empty array"),
            ({"message": "Q", "domains": [""]}, "empty element"),
            ({"message": "Q", "domains": ["  "]}, "whitespace element"),
            ({"message": "Q", "domains": "ATTENDANCE"}, "a bare string"),
            ({"message": "Q", "domains": [1]}, "a non-string element"),
            ({"message": "Q", "domains": ["A"] * 33}, "more codes than any catalog"),
        ):
            result = client.post("/chat/api/message", json=payload)
            assert result.status_code == 422, (why, result.status_code, result.text)
            assert result.json()["error"]["fields"], why


def test_the_bff_does_not_validate_catalog_MEMBERSHIP():
    """A code this build has never heard of is FORWARDED, not rejected locally.

    Which codes exist is per-school data and search owns it. A membership list
    here would be a second copy to keep in step with a database — the mistake
    this file already has a scar from, with the Czech role slugs — and it would
    reject a code a school added five minutes ago.
    """
    fake = FakeSearchClient(
        response({"answer": "A", "outcome": "answer", "answer_source": "rag",
                  "suggestions": [], "sources": [], "language": "cs",
                  "duration_ms": 1})
    )
    app.dependency_overrides[get_search_client] = lambda: fake

    with TestClient(app) as client:
        result = client.post("/chat/api/message",
                             json={"message": "Q", "domains": ["A_BRAND_NEW_AREA"]})

    app.dependency_overrides.clear()
    assert result.status_code == 200
    assert fake.last_json["domains"] == ["A_BRAND_NEW_AREA"]


def test_source_mapper_drops_internal_fields_and_unsafe_urls():
    mapped = map_sources(
        [
            {
                "file_name": "učebnice.pdf",
                "file_uuid": "secret",
                "file_item_uuid": "secret-item",
                "page": 12,
                "url": "javascript:alert(1)",
            }
        ]
    )
    assert [item.model_dump() for item in mapped] == [
        {"title": "učebnice.pdf", "page": 12, "url": None}
    ]
