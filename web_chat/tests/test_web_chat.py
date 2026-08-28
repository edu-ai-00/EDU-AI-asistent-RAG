# last change in code by Jan Drozd and Martin Safranek (2026-08-18).
import uuid

import httpx
from fastapi.testclient import TestClient

from web_chat import app, get_search_client, map_sources


class FakeSearchClient:
    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.last_json = None

    async def post(self, _url: str, json: dict):
        self.last_json = json
        if self.error:
            raise self.error
        return self.response


def response(payload: dict, status_code: int = 200) -> httpx.Response:
    request = httpx.Request("POST", "http://search/search")
    return httpx.Response(status_code, json=payload, request=request)


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
    with TestClient(app) as client:
        result = client.post("/chat/api/message", json={"message": "   "})

    assert result.status_code == 400
    assert result.json()["error"]["code"] == "invalid_message"


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
