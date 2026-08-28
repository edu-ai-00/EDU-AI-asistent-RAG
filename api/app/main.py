# last change in code by Jan Drozd and Martin Safranek (2026-08-19).
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response
from pathlib import Path
import uuid
import requests
import os
import json
import re
import unicodedata
import validators
from urllib.parse import urlparse, urljoin, quote
from bs4 import BeautifulSoup
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# root_path: nginx serves this app under /api/ and strips the prefix, so without
# it the docs page asks for /openapi.json at the domain root — where nothing is
# mounted — and renders "Failed to load API definition". Routing is unaffected.
app = FastAPI(title="edu_ai Ingestion API", docs_url="/docs", root_path="/api")

# CORS: CLOSED by default since 2026-08-13, same change as the search service. It
# was "*" for tools/upload_test.html; nothing in production calls this from a
# browser cross-origin. It matters more here than there: nginx puts Basic auth in
# front of /api/, and Basic auth is exactly the kind of credential a browser
# attaches by itself — so "*" plus a later allow_credentials=True would be a real
# hole, not a theoretical one.
#
# Set CORS_ORIGINS=https://a,https://b to open it for named origins.
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"],
)

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

LANGFLOW_URL = os.getenv("LANGFLOW_URL")
LANGFLOW_API_KEY = os.getenv("LANGFLOW_API_KEY")

# Search service — source of the document-type taxonomy (the gateway has NO DB
# access by design; the search service reads the číselník and exposes it).
SEARCH_URL = os.getenv("SEARCH_URL", "http://edu_ai_search:8090")
# Search moved its whole surface under /v1 on 2026-08-13 (only /health stayed
# unversioned, which this service never calls). Spelled out here rather than
# folded into SEARCH_URL so the version is visible at every call site.
SEARCH_V1 = f"{SEARCH_URL}/v1"

# Číselník proxy cache (per path): {"ts": epoch, "data": ...}. 60 s TTL keeps
# číselník edits near-instant in the upload UI while the search service sees at
# most one request per list a minute. On failure the last known (stale) value
# is served — číselníky are a soft dependency, ingestion must never block on them.
_CODELIST_TTL_S = 60
_codelist_cache: dict[str, dict] = {}


def _fetch_from_search(path: str, key: str):
    """GET {SEARCH_V1}{path}, return json[key]; TTL-cached, stale on failure."""
    import time
    now = time.time()
    entry = _codelist_cache.get(path)
    if entry and entry["data"] is not None and now - entry["ts"] < _CODELIST_TTL_S:
        return entry["data"]
    try:
        resp = requests.get(f"{SEARCH_V1}{path}", timeout=5)
        resp.raise_for_status()
        data = resp.json().get(key)
        _codelist_cache[path] = {"ts": now, "data": data}
        return data
    except Exception as e:
        logger.warning("%s fetch failed (%s) — serving stale/none", path, e)
        return entry["data"] if entry else None


def _validate_principal(legal_entity: str, user_id: str, visibility: str) -> dict:
    """Who is ingesting, for which entity, and how far the document will reach.

    Answered by the search service because this one cannot: edu_ai_api is
    deliberately off the `data` network, so the internet-facing service holds no
    database credentials. Same route as the document-type check above — and
    deliberately NOT the same failure mode. That one is soft: a taxonomy that
    cannot be reached lets the value through, because the worst case is an
    unclassified document. This one is hard. An entity that cannot be verified is
    refused, because accepting it would file somebody's document under an
    unverified owner, and that is the isolation boundary itself.

    Not cached either. A deactivated user must stop being able to ingest at once,
    not within a minute.
    """
    try:
        resp = requests.get(f"{SEARCH_V1}/ingest-principal",
                            params={"legal_entity": legal_entity,
                                    "user_id": user_id,
                                    "visibility": visibility},
                            timeout=5)
    except Exception as e:
        logger.warning("principal validation unreachable (%s)", e)
        raise HTTPException(
            status_code=503,
            detail="Cannot verify the legal entity and user — ingestion refused")
    if resp.status_code >= 400:
        # search wraps its errors as {"error": {code, message, retryable}} since
        # 2026-08-13; `detail` is the pre-envelope shape, kept so a rollback of
        # either service does not turn a readable refusal into "invalid principal"
        try:
            body = resp.json()
            detail = (body.get("error") or {}).get("message") or body.get("detail")
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=400, detail=detail or "invalid principal")
    return resp.json()


def _get_document_types() -> list | None:
    return _fetch_from_search("/document-types", "document_types")


def _get_code_lists() -> dict | None:
    return _fetch_from_search("/code-lists", "code_lists")

# Extraction flows — one per source type
FLOW_ID_WEB = os.getenv("LANGFLOW_FLOW_ID_WEB")
FLOW_ID_PDF = os.getenv("LANGFLOW_FLOW_ID_PDF")
FLOW_ID_OFFICE = os.getenv("LANGFLOW_FLOW_ID_OFFICE")
FLOW_ID_QA = os.getenv("LANGFLOW_FLOW_ID_QA")  # QA CSV → qa_* tables (EDU_AI_QA)

# Vector search (Langflow flow)

# Node id in the ingestion flows that receives the pipeline_input tweak. Must
# match the APIPipelineInput node id in the extraction flows; overridable so a
# renamed flow node doesn't silently break ingestion.
PIPELINE_INPUT_NODE = os.getenv("LANGFLOW_PIPELINE_INPUT_NODE", "APIPipelineInput-tfiG6")

# Per-flow input node ids. Node ids are UNIQUE per flow (…-PDF/-WEB/-OFF/-QA/-MD
# suffixes) so Langflow's vertex cache — keyed by bare node id with no flow
# scope — can never serve one flow's cached pipeline_input to another flow
# (cross-flow cache poisoning: child PDFs used to receive the parent page's
# input). Falls back to the legacy shared PIPELINE_INPUT_NODE when unset.
PIPELINE_INPUT_NODE_BY_FLOW = {
    FLOW_ID_WEB: os.getenv("LANGFLOW_PIPELINE_INPUT_NODE_WEB", PIPELINE_INPUT_NODE),
    FLOW_ID_PDF: os.getenv("LANGFLOW_PIPELINE_INPUT_NODE_PDF", PIPELINE_INPUT_NODE),
    FLOW_ID_OFFICE: os.getenv("LANGFLOW_PIPELINE_INPUT_NODE_OFFICE", PIPELINE_INPUT_NODE),
    FLOW_ID_QA: os.getenv("LANGFLOW_PIPELINE_INPUT_NODE_QA", PIPELINE_INPUT_NODE),
}

# -------------------------------------------------------------------
# CONFIG VALIDATION — lazy, never at import time
# -------------------------------------------------------------------
# Missing config must NOT crash the process. A crash-looping container
# cannot answer "what is wrong with me?", so the operator is left reading
# nginx 502s and `docker logs` instead of one clear /health payload. The
# checks below run per-request: /health always answers, and only the routes
# that genuinely need Langflow fail — with 503 and a named variable.

def _missing_config() -> list[str]:
    """Names of required env vars that are unset or still a CHANGE_ME placeholder."""
    return [
        name
        for name, value in {
            "LANGFLOW_URL": LANGFLOW_URL,
            "LANGFLOW_API_KEY": LANGFLOW_API_KEY,
            "LANGFLOW_FLOW_ID_PDF": FLOW_ID_PDF,
            "LANGFLOW_FLOW_ID_OFFICE": FLOW_ID_OFFICE,
            "LANGFLOW_FLOW_ID_WEB": FLOW_ID_WEB,
            "LANGFLOW_FLOW_ID_QA": FLOW_ID_QA,
        }.items()
        if not value or str(value).startswith("CHANGE_ME")
    ]


def _require_langflow() -> tuple[str, str]:
    """Return (url, api_key) or raise 503 naming what the operator must set."""
    missing = [
        name
        for name, value in (("LANGFLOW_URL", LANGFLOW_URL), ("LANGFLOW_API_KEY", LANGFLOW_API_KEY))
        if not value or str(value).startswith("CHANGE_ME")
    ]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Service not configured: {', '.join(missing)} is not set (see /health)",
        )
    return LANGFLOW_URL, LANGFLOW_API_KEY


@app.get("/health")
def health() -> dict:
    """Liveness + configuration readiness. Always 200 so the payload is readable;
    `status` distinguishes a healthy instance from a misconfigured one."""
    missing = _missing_config()
    return {
        "status": "degraded" if missing else "ok",
        "service": "edu_ai_api",
        "missing_config": missing,
    }


for _name in _missing_config():
    logger.warning("Config incomplete: %s is not set (see GET /health)", _name)


# Source type → Flow ID routing
EXTRACTION_FLOW_MAP = {
    "web": FLOW_ID_WEB,
    "pdf": FLOW_ID_PDF,
    "docx": FLOW_ID_OFFICE,
    "pptx": FLOW_ID_OFFICE,
    "xlsx": FLOW_ID_OFFICE,
    "qa": FLOW_ID_QA,          # QA CSV upload → EDU_AI_QA flow
}


def get_extraction_flow_id(source_type: str) -> str:
    """Route source type to the correct Langflow extraction flow."""
    flow_id = EXTRACTION_FLOW_MAP.get(source_type)
    if not flow_id:
        raise ValueError(
            f"No extraction flow configured for source type: {source_type}"
        )
    return flow_id


UPLOAD_DIR = Path("/data/uploads")

# Supported file extensions for direct upload
SUPPORTED_FILE_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".csv"}

# File extensions for child link crawling
FILE_EXTENSIONS_DOWNLOADABLE = {".pdf", ".docx", ".pptx", ".xlsx"}
FILE_EXTENSIONS_SKIP = {
    ".zip", ".rar", ".tar", ".gz",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv",
    ".css", ".js", ".json", ".xml",
}

MAX_CHILD_LINKS = 5

# QA pack name: an identifier, not a label. Kept ASCII so the same string
# works in a URL, a filename, a JSON payload and a shell argument unquoted.
_PACK_ID_RX = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# -------------------------------------------------------------------
# HELPERS
# -------------------------------------------------------------------

def normalize_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = name.replace(" ", "_")
    return name


def normalize_url(url: str) -> str:
    url = url.strip()

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)

    if not parsed.netloc:
        raise ValueError("Invalid URL")

    return url


# ── HTML encoding detection ──────────────────────────────────
# requests' response.text is unreliable: when the HTTP header carries no
# charset (common for text/html) requests DEFAULTS to ISO-8859-1 and ignores
# the HTML <meta charset>, so a Czech UTF-8/cp1250 page decodes to mojibake.
# We must decode from the RAW BYTES here — once a mis-decoded string is saved
# it becomes "valid UTF-8" mojibake that no downstream step can recover.
_HTML_BOMS = [
    (b"\x00\x00\xfe\xff", "utf-32-be"), (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"),
]
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?\s*([a-zA-Z0-9_\-]+)""", re.I)
_CZ_LETTERS = set("áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ")


def _cz_score(text: str) -> int:
    """Higher = more Czech-plausible. Rewards CZ letters, punishes C1/control chars."""
    cz = sum(c in _CZ_LETTERS for c in text)
    bad = sum(1 for c in text
              if (ord(c) < 32 and c not in "\t\n\r") or 0x80 <= ord(c) <= 0x9f)
    return cz - 5 * bad


def _try_decode(raw: bytes, enc: str):
    try:
        return raw.decode(enc)
    except (UnicodeDecodeError, LookupError):
        return None


def decode_html(raw: bytes, header_charset: str | None = None) -> str:
    """Decode raw page bytes → NFC unicode, precedence: BOM → HTTP header charset
    → HTML <meta charset> → strict UTF-8 → Czech-aware scoring (cp1250 /
    iso-8859-2 / cp1252 / latin-1). Mirrors the QA QaEncodingNormalizer."""
    for bom, enc in _HTML_BOMS:
        if raw.startswith(bom):
            t = _try_decode(raw, enc)
            if t is not None:
                return unicodedata.normalize("NFC", t)
    if header_charset:
        t = _try_decode(raw, header_charset)
        if t is not None:
            return unicodedata.normalize("NFC", t)
    m = _META_CHARSET_RE.search(raw[:4096])
    if m:
        t = _try_decode(raw, m.group(1).decode("ascii", "ignore"))
        if t is not None:
            return unicodedata.normalize("NFC", t)
    t = _try_decode(raw, "utf-8")
    if t is not None:
        return unicodedata.normalize("NFC", t)
    best = None  # (text, score)
    for enc in ("cp1250", "iso-8859-2", "cp1252", "latin-1"):
        t = _try_decode(raw, enc)
        if t is None:
            continue
        s = _cz_score(t)
        if best is None or s > best[1]:
            best = (t, s)
    return unicodedata.normalize("NFC", best[0] if best else raw.decode("latin-1"))


def fetch_html(url: str) -> str:
    """Fetch an HTML page and decode it to clean UTF-8 (NFC) from raw bytes."""
    response = requests.get(url, timeout=30)

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {url}")

    # only trust an EXPLICIT header charset (not requests' ISO-8859-1 default)
    header_charset = None
    m = re.search(r"charset=([a-zA-Z0-9_\-]+)", response.headers.get("content-type", ""), re.I)
    if m:
        header_charset = m.group(1)
    return decode_html(response.content, header_charset)


def fetch_file(url: str) -> bytes:
    """Download a binary file. Returns raw bytes."""
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response.content


def clean_text(text: str) -> str:
    return text.replace("​", "").strip()


def detect_source_type(file_path: str) -> str:
    """Detect source type from file extension."""
    ext = Path(file_path).suffix.lower()
    type_map = {
        ".pdf": "pdf",
        ".docx": "docx",
        ".pptx": "pptx",
        ".xlsx": "xlsx",
        ".txt": "txt",
        ".html": "web",
        ".htm": "web",
        ".csv": "qa",      # QA question/answer pack
    }
    return type_map.get(ext, "unknown")


# Map an HTTP Content-Type to a downloadable file extension — used to detect
# extensionless file URLs (e.g. msmt.gov.cz/file/65358_2_1/ serving a PDF).
CONTENT_TYPE_EXT = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}


def resolve_file_ext(url: str, content_type: str) -> str | None:
    """If a URL points directly at a downloadable file, return its extension
    (by URL suffix first, else by Content-Type); return None for an HTML page."""
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in FILE_EXTENSIONS_DOWNLOADABLE:
        return ext
    ct = (content_type or "").split(";")[0].strip().lower()
    mapped = CONTENT_TYPE_EXT.get(ct)
    if mapped and mapped in FILE_EXTENSIONS_DOWNLOADABLE:
        return mapped
    return None


def url_to_safe_filename(url: str) -> str:
    """Convert a URL to a safe filename (max 80 chars)."""
    return normalize_filename(
        url.replace("https://", "").replace("http://", "").replace("/", "_")
    )[:80]


# -------------------------------------------------------------------
# LANGFLOW API CALLER
# -------------------------------------------------------------------

def call_langflow(flow_id: str, pipeline_input: dict, timeout: int = 600,
                  session_id: str | None = None) -> dict:
    """Call a Langflow flow via API and return the response JSON.

    session_id isolates the run: without it Langflow reuses ONE session per flow,
    so several files ingested in a single request (parent + level-1 children) all
    share the same session — and the WEB flow's per-node output cache from the
    first file is served to the next, which crashes the 2nd/3rd file. A unique
    session per file (file_uuid) gives each its own cache scope.
    """
    base_url, api_key = _require_langflow()
    input_node = PIPELINE_INPUT_NODE_BY_FLOW.get(flow_id, PIPELINE_INPUT_NODE)
    payload = {
        "output_type": "text",
        "input_type": "text",
        "input_value": "trigger",
        "tweaks": {
            input_node: {
                "input_value": json.dumps(pipeline_input)
            }
        }
    }
    if session_id:
        payload["session_id"] = session_id

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
    }

    r = requests.post(
        f"{base_url}/api/v1/run/{flow_id}?stream=false",
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    r.raise_for_status()

    return r.json()


def _extract_pipeline_status(response_json: dict):
    """Best-effort pull of the QAFinalResult status (surfaced via ChatOutput) out
    of a Langflow /run response. Returns the parsed dict, or None (e.g. for the
    document flows, which have no QAFinalResult)."""
    texts: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "text" and isinstance(v, str):
                    texts.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(response_json)
    for t in texts:
        if "json_parse" not in t:
            continue
        i = t.find("{")
        while i != -1:                       # bracket-match the JSON object holding the status
            depth = 0
            for j in range(i, len(t)):
                if t[j] == "{":
                    depth += 1
                elif t[j] == "}":
                    depth -= 1
                    if depth == 0:
                        cand = t[i:j + 1]
                        if "json_parse" in cand:
                            try:
                                return json.loads(cand)
                            except Exception:
                                try:
                                    return json.loads(cand.encode().decode("unicode_escape"))
                                except Exception:
                                    pass
                        break
            i = t.find("{", i + 1)
    return None


# -------------------------------------------------------------------
# LINK EXTRACTION (from <main> HTML — for child crawling)
# -------------------------------------------------------------------

# Archive / listing / index paths that carry navigation, not content
# (WordPress tag/category/author/paginated/feed archives + the bare site root).
LOW_VALUE_PATH_SEGMENTS = (
    "/tag/", "/category/", "/categories/", "/author/", "/authors/",
    "/page/", "/feed", "/rubrika/", "/stitek/", "/archiv", "/search",
    "/wp-json", "/wp-admin", "/wp-login",
)

# Minimum main-content length (chars, after stripping chrome) for a crawled
# HTML child to be worth ingesting — filters near-empty index pages.
MIN_CHILD_CONTENT_CHARS = 500


def _is_low_value_url(parsed) -> bool:
    """True for archive/listing/index pages and the bare site root — these are
    link lists, not content. #1 of the crawl-scope filter."""
    path = (parsed.path or "/").rstrip("/") or "/"
    ql = (parsed.query or "").strip().lower()
    # query-routed junk: RSS feeds, author/profile modules and pagination carry
    # no real content — on a query-routed CMS their identity is in the query, so
    # judge them there, not on the shared /index.php path.
    if path.lower().endswith("rss.php") or "rss" in ql:
        return True
    if "m=profile" in ql or "&page=" in ql or ql.startswith("page="):
        return True
    # A router endpoint (/, /index.php, /index.html) is a bare listing ONLY when
    # it carries no query. Query-routed CMSs (…/index.php?p=o-studiu, ?a=article)
    # put the page identity in the QUERY string, so those ARE content pages —
    # treating them as low-value dropped every article on skola-nova-ves.example.
    if path == "/" or path.lower() in ("/index.php", "/index.html"):
        return not ql
    low = path.lower() + "/"
    return any(seg in low for seg in LOW_VALUE_PATH_SEGMENTS)


def _main_text_len(html: str) -> int:
    """Length of the page's main text after dropping nav/menu/chrome — mirrors
    the web flow's HtmlToMarkdown scoping. Used to skip near-empty index pages
    that carry no archive-style URL pattern. Fails open (never drops on error)."""
    try:
        soup = BeautifulSoup(html, "lxml")
        root = soup.find("main") or soup.body or soup
        for tag in root.find_all(["nav", "header", "footer", "aside", "script", "style", "form"]):
            tag.decompose()
        return len(root.get_text(" ", strip=True))
    except Exception:
        return len(html or "")


# the SAME reduction the web flow's HtmlReadabilityToMarkdown applies before
# chunking — child links must come from the same "main content" the chunks
# come from. Tag-based filtering alone misses non-semantic themes (ZŠ
# Borová: the menu is a bare ul.top_menu in div.header, no <nav> anywhere),
# and its 50+ menu links starved the child cap before the content's PDF link.
_LINKS_ALWAYS_DROP = ["script", "style", "noscript", "iframe", "svg", "form",
                      "nav", "header", "footer", "aside"]
_LINKS_DENSITY_THRESHOLD = 0.65
_LINKS_DENSITY_MIN_LINKS = 3


def _prune_navigation(root) -> None:
    """Iterative link-density pruning, mirroring the web flow: a block whose
    text is mostly link text IS navigation, whatever tag it lives in."""
    for tag in root.find_all(_LINKS_ALWAYS_DROP):
        if not getattr(tag, "decomposed", False):
            tag.decompose()
    for _ in range(4):
        removed = 0
        for el in root.find_all(["div", "ul", "ol", "table", "td",
                                 "p", "section", "span"]):
            if getattr(el, "decomposed", False):
                continue
            text = el.get_text(" ", strip=True)
            tlen = len(text)
            if tlen < 40:
                continue
            links = el.find_all("a")
            llen = sum(len(a.get_text(" ", strip=True)) for a in links)
            if len(links) >= _LINKS_DENSITY_MIN_LINKS \
                    and llen / max(tlen, 1) > _LINKS_DENSITY_THRESHOLD:
                el.decompose()
                removed += 1
        if not removed:
            break


def extract_links(html: str, source_url: str, max_links: int = MAX_CHILD_LINKS) -> list[dict]:
    """Extract same-domain links from the main content area."""
    soup = BeautifulSoup(html, "lxml")

    main = soup.find("main") or soup.body
    if not main:
        return []

    _prune_navigation(main)

    source_parsed = urlparse(source_url)
    source_domain = source_parsed.netloc.lower()

    seen = set()
    links = []

    for a_tag in main.find_all("a", href=True):
        href = a_tag["href"].strip()

        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue

        full_url = urljoin(source_url, href)
        parsed = urlparse(full_url)

        if parsed.netloc.lower() != source_domain:
            continue

        # canonical URL (lowercase scheme+host, drop query/fragment, strip
        # trailing slash) so the same page reached via different seeds yields
        # one source_url — lets the metadata-insert dedup (deactivate same
        # source_url) match reliably across seeds.
        canon_path = parsed.path.rstrip("/") or "/"
        clean_url = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{canon_path}"
        # Preserve the query string: on query-routed CMSs (index.php?p=…, ?a=…)
        # the page identity lives THERE, so dropping it collapsed every article
        # onto one bare /index.php (deduped to one, then fetched WITHOUT the
        # query → the homepage). Normal path-routed sites have no query, so this
        # is a no-op for them.
        if parsed.query:
            clean_url += "?" + parsed.query
        if clean_url.rstrip("/") == source_url.rstrip("/"):
            continue

        if clean_url in seen:
            continue
        seen.add(clean_url)

        # #1 crawl-scope filter: skip archive/listing/index pages (link lists,
        # not content). PDF/office file links are never low-value → keep them.
        ext = Path(parsed.path.lower()).suffix
        if ext not in FILE_EXTENSIONS_DOWNLOADABLE and _is_low_value_url(parsed):
            continue

        if ext in FILE_EXTENSIONS_SKIP:
            continue

        if ext in FILE_EXTENSIONS_DOWNLOADABLE:
            link_type = "file"
        else:
            link_type = "page"

        anchor_text = clean_text(a_tag.get_text(" ", strip=True))

        links.append({
            "url": clean_url,
            "type": link_type,
            "anchor_text": anchor_text[:200],
        })

    # content FILES first (pdf/docx… are why the level-1 crawl exists), pages
    # after, document order within each group — THEN the cap. Page links must
    # never starve a file link out of the budget.
    links.sort(key=lambda l: l["type"] != "file")
    return links[:max_links]


# -------------------------------------------------------------------
# FETCH & SAVE CHILD PAGES/FILES
# -------------------------------------------------------------------

def fetch_and_save_children(
    child_links: list[dict],
    processing_uuid: str,
) -> list[dict]:
    """
    Fetch each child link and save to filesystem.
    Returns list of saved file descriptors (skips failed fetches).
    Each child gets the same processing_uuid but its own file_uuid.
    """
    saved = []

    for link in child_links:
        child_url = link["url"]
        link_type = link["type"]
        file_uuid = str(uuid.uuid4())

        try:
            if link_type == "file":
                # Download binary file (PDF, DOCX, etc.)
                content = fetch_file(child_url)

                filename = Path(urlparse(child_url).path).name or "download"
                safe_filename = normalize_filename(filename)
                # Insert _CH before extension
                stem = Path(safe_filename).stem
                ext = Path(safe_filename).suffix
                final_filename = f"{file_uuid}_{stem}_CH{ext}"

                file_path = UPLOAD_DIR / final_filename

                with open(file_path, "wb") as f:
                    f.write(content)

            else:
                # Fetch HTML page
                html = fetch_html(child_url)

                # #2 content gate: skip near-empty index pages that slipped
                # past the URL-pattern filter (main text below threshold).
                main_len = _main_text_len(html)
                if main_len < MIN_CHILD_CONTENT_CHARS:
                    logger.info(
                        f"[CHILD SKIP low-content] url={child_url} main_chars={main_len}"
                    )
                    continue

                base_name = url_to_safe_filename(child_url)
                final_filename = f"{file_uuid}_{base_name}_CH.html"

                file_path = UPLOAD_DIR / final_filename

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(html)

            source_type = detect_source_type(str(file_path))

            saved.append({
                "file_uuid": file_uuid,
                "file_name": final_filename,
                "file_path": str(file_path),
                "source_url": child_url,
                "source_type": source_type,
                "page_role": "child",
                "anchor_text": link.get("anchor_text", ""),
            })

            logger.info(
                f"[CHILD FETCH OK] url={child_url} "
                f"file_uuid={file_uuid} type={source_type}"
            )

        except Exception as e:
            logger.warning(
                f"[CHILD FETCH SKIP] url={child_url} error={e}"
            )
            continue

    return saved


# -------------------------------------------------------------------
# PROCESS ONE FILE VIA LANGFLOW (synchronous)
# -------------------------------------------------------------------

def process_file(
    file_uuid: str,
    file_name: str,
    file_path: str,
    source_type: str,
    source_url: str | None,
    page_role: str,
    processing_uuid: str,
    parent_file_uuid: str | None,
    user_name: str,
    legal_entity: str,
    user_id: str,
    visibility: str,
    document_type: str | None,
    document_language: str | None,
    school_subject: str | None,
    school_subject_grades: list[str] | None = None,   # DECLARED grade set (číselník kódy)
    audience_roles: list[str] | None = None,
    domains: list[str] | None = None,
    user_defined_pack_id: str | None = None,
    qa_mode: str = "merge",          # QA packs only: what an ABSENT CSV row means
    valid_from: str | None = None,   # ISO date or None — document validity window
    valid_to: str | None = None,
) -> dict:
    """
    Call the appropriate Langflow extraction flow for one file.
    Returns a result dict with status and details.
    """
    # DECLARED grades are a set now. The QA pack triple is single-grade by nature,
    # so the pack id uses the FIRST declared grade; the full set rides as a JSON
    # string (like document_domain_orig) into document_metadata.school_grade_user.
    _grades = [g for g in (school_subject_grades or []) if g]
    _primary_grade = _grades[0] if _grades else None
    try:
        flow_id = get_extraction_flow_id(source_type)
    except ValueError as e:
        return {
            "file_uuid": file_uuid,
            "source_url": source_url,
            "source_type": source_type,
            "page_role": page_role,
            "status": "error",
            "error": str(e),
        }

    pipeline_input = {
        "pipeline_input": {
            "processing_uuid": processing_uuid,
            "file_uuid": file_uuid,
            "file_name": file_name,
            "file_path": file_path,
            "source_type": source_type,

            # MD manifest fields for QA packs (document flows ignore them).
            # pack_id is the REPLACEMENT KEY: uploading a pack of the same name
            # replaces that school's previous version of it. It is therefore
            # REQUIRED and passed through VERBATIM — never derived (2026-08-18).
            # The old `lang-subject-grade` derivation gave every plain upload
            # "cs-core", so a school's second QA upload silently replaced its
            # first — and before the entity-scope fix, other schools' packs too.
            # schema_version + per-pack thresholds still default here.
            "user_defined_pack_id": user_defined_pack_id,
            "pack_id": user_defined_pack_id,
            # what an absent CSV row means; the QA reader validates it again and
            # the insert acts on it. Ignored by every other source type.
            "qa_mode": qa_mode,
            "schema_version": 1,
            "t_answer": 0.60,
            "t_suggest": 0.30,
            "m_margin": 0.08,

            "user_name": user_name,

            # WHOSE it is, WHO ingested it, HOW FAR it reaches. Verified against
            # the register before this call; the flows write all three onto every
            # row they insert, so no table has to be joined to find out.
            "legal_entity_code": legal_entity,
            "user_id": user_id,
            "visibility": visibility,

            "document_type": document_type,
            "document_language": document_language,
            "school_subject": school_subject,
            # single primary grade — kept only for the QA pack manifest
            "school_subject_grade": _primary_grade,
            # DECLARED audience (>=1 of STUDENT/TEACHER/PARENT) — the flows
            # write it onto document_metadata AND every chunk for the search
            # role filter (init 127)
            # JSON STRING (not a list): it rides through the DataFrame pipeline
            # and a bare list cell makes DataFrameOperations' hashing choke
            # ("unhashable type: list"). The insert components json.loads it.
            "audience_roles": json.dumps(audience_roles or []),
            # DECLARED domains (taxonomy_rules DOMAIN codes) — carried like
            # audience_roles as a JSON string; must survive ALL PDF/WEB/OFFICE
            # extraction branches. Parse feeds it as the USER domain (overrides
            # the LLM classification); unknown codes are dropped at insert.
            "document_domain_orig": json.dumps(domains or []),
            # DECLARED grade SET (číselník codes) — carried like document_domain_orig
            # as a JSON string; the parser feeds it as school_grade_user (the effective
            # doc-level grades). Survives all PDF/WEB/OFFICE branches.
            "document_grade_orig": json.dumps(_grades),
            # DOCUMENT VALIDITY (ISO date strings or None) — validated in /v1/ingest;
            # the insert components write them onto document_metadata, chunks,
            # elements and tables; search hard-filters against CURRENT_DATE.
            "valid_from": valid_from,
            "valid_to": valid_to,

            "record_type": "document",
            "source_url": source_url,
            "page_role": page_role,
            "parent_file_uuid": parent_file_uuid,
        }
    }

    try:
        response_json = call_langflow(flow_id, pipeline_input, session_id=file_uuid)

        pipeline_status = _extract_pipeline_status(response_json)
        return {
            "file_uuid": file_uuid,
            "source_url": source_url,
            "source_type": source_type,
            "page_role": page_role,
            # OK from Langflow's side, but a soft per-stage failure (e.g. QA/vector
            # insert) is reported by the pipeline itself → reflect it here.
            "status": "error" if (pipeline_status or {}).get("status") == "PARTIAL_FAILURE" else "ok",
            "langflow_session_id": response_json.get("session_id"),
            "pipeline_status": pipeline_status,
        }

    except requests.HTTPError as e:
        error_text = e.response.text if e.response else str(e)
        logger.error(
            f"[LANGFLOW ERROR] file_uuid={file_uuid} error={error_text[:500]}"
        )
        return {
            "file_uuid": file_uuid,
            "source_url": source_url,
            "source_type": source_type,
            "page_role": page_role,
            "status": "error",
            "error": f"Langflow error: {error_text[:200]}",
        }

    except Exception as e:
        logger.error(
            f"[LANGFLOW ERROR] file_uuid={file_uuid} error={e}"
        )
        return {
            "file_uuid": file_uuid,
            "source_url": source_url,
            "source_type": source_type,
            "page_role": page_role,
            "status": "error",
            "error": str(e),
        }


# -------------------------------------------------------------------
# INGEST API
# -------------------------------------------------------------------

@app.get("/v1/document-types")
def document_types():
    """Allowed document types for the upload UI select menu.

    Pass-through of the search service's /document-types (the single DB source),
    TTL-cached 60 s. 503 only when the taxonomy has never been fetchable.
    """
    types = _get_document_types()
    if types is None:
        raise HTTPException(status_code=503, detail="Document-type taxonomy unavailable")
    return {"document_types": types}


@app.get("/v1/domains")
def domains(legal_entity: str | None = None):
    """Allowed DOMAIN codes (school-system situations) for the upload UI
    multiselect — pass-through of the search service's /domains. Optionally
    PER SCHOOL: with legal_entity the set is resolved nearest-owner-up (the
    school's own domains, else the parent's, else global) — same as subjects/grades."""
    from urllib.parse import quote
    path = "/domains" + (f"?legal_entity={quote(legal_entity)}" if legal_entity else "")
    data = _fetch_from_search(path, "domains")
    if data is None:
        raise HTTPException(status_code=503, detail="Domain taxonomy unavailable")
    return {"domains": data}


@app.get("/v1/entities")
def entities():
    """Active legal entities — pass-through of the search service's /entities for
    the ingest form. Hard dependency: without the register the form cannot offer a
    valid choice, and guessing one is how a document ends up under the wrong owner."""
    data = _fetch_from_search("/entities", "entities")
    if data is None:
        raise HTTPException(status_code=503, detail="Entity register unavailable")
    return {"entities": data}


@app.get("/v1/staff-users")
def staff_users(legal_entity: str):
    """Active staff users of one entity, for the ingest form's second choice.
    Not cached: a deactivated user has to disappear from the list at once."""
    try:
        resp = requests.get(f"{SEARCH_V1}/staff-users",
                            params={"legal_entity": legal_entity}, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("staff-users fetch failed (%s)", e)
        raise HTTPException(status_code=503, detail="Staff-user register unavailable")


@app.get("/v1/validity-periods")
def validity_periods(legal_entity: str | None = None):
    """Validity periods of the entity's organisation — pass-through of the
    search service's /validity-periods (a unit resolves to its organisation).
    The upload test page lists them for the selected entity (read-only)."""
    from urllib.parse import quote
    path = ("/validity-periods"
            + (f"?legal_entity={quote(legal_entity)}" if legal_entity else ""))
    periods = _fetch_from_search(path, "validity_periods")
    if periods is None:
        raise HTTPException(status_code=503, detail="Validity periods unavailable")
    return {"validity_periods": periods}


@app.get("/v1/code-lists")
def code_lists(legal_entity: str | None = None):
    """Generic číselníky (languages ISO 639-1 / school subjects / school grades)
    for UI select menus — pass-through of the search service's /code-lists,
    TTL-cached 60 s (per entity: the cache keys on the full path). With
    legal_entity the sets are the ENTITY'S resolved codebooks (per-unit since
    init 120) — the upload form must offer the grades of the entity it
    ingests for, not the founding template."""
    from urllib.parse import quote
    path = "/code-lists" + (f"?legal_entity={quote(legal_entity)}" if legal_entity else "")
    lists = _fetch_from_search(path, "code_lists")
    if lists is None:
        raise HTTPException(status_code=503, detail="Code lists unavailable")
    return {"code_lists": lists}


@app.get("/v1/qa-packs")
def qa_packs(legal_entity: str):
    """Active QA packs of one entity — pass-through of the search service's
    /qa-packs. Answers "what can I download and edit"; not cached, because a
    pack that was just replaced has to show its new version at once."""
    packs = _fetch_from_search(f"/qa-packs?legal_entity={quote(legal_entity)}",
                               "qa_packs")
    if packs is None:
        raise HTTPException(status_code=503, detail="QA pack list unavailable")
    return {"qa_packs": packs}


@app.get(
    "/v1/qa-export",
    # Declared explicitly because FastAPI assumes application/json for every
    # response, and this one is a CSV file — an OpenAPI consumer would otherwise
    # generate a client that parses the download as JSON.
    response_class=Response,
    responses={
        200: {"content": {"text/csv": {"schema": {"type": "string"}}},
              "description": "The pack as a CSV file (UTF-8 with BOM, ';' "
                             "delimited), ready to edit and upload back."},
        404: {"description": "No active pack of that name for this entity."},
    },
)
def qa_export(legal_entity: str, pack_id: str):
    """One QA pack as a CSV download, for editing and re-uploading.

    The body is composed by the search service (it owns the format the ingest
    reader accepts, so the two cannot drift); this adds only what makes a browser
    save it as a file. Not a JSON pass-through — the response IS the file, so it
    is forwarded as bytes and never re-encoded.

    Deliberately no `dry_run`-style caching or TTL: the point of the download is
    that it matches the database right now.
    """
    if not _PACK_ID_RX.match((pack_id or "").strip()):
        raise HTTPException(status_code=400, detail={
            "error": f"pack_id {pack_id!r} is not allowed",
            "allowed": "ASCII letters, digits, '-' and '_', max 64 chars"})
    try:
        resp = requests.get(f"{SEARCH_V1}/qa-export",
                            params={"legal_entity": legal_entity,
                                    "pack_id": pack_id},
                            timeout=30)
    except Exception as e:
        logger.warning("/qa-export fetch failed (%s)", e)
        raise HTTPException(status_code=503, detail="QA export unavailable")
    # 404 (unknown pack / not this entity's) is the caller's answer, not a
    # gateway fault — forward it instead of turning everything into a 503.
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=(
            f"no active QA pack '{pack_id}' for '{legal_entity}'"))
    if not resp.ok:
        logger.warning("/qa-export returned HTTP %s", resp.status_code)
        raise HTTPException(status_code=503, detail="QA export unavailable")
    # filename carries the entity too: a curator downloading the same pack name
    # from two schools would otherwise get two files called the same thing.
    fname = f"qa_{legal_entity}_{pack_id}.csv"
    return Response(
        content=resp.content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/v1/ui-labels")
def ui_labels(scope: str):
    """Interface copy for one scope (edu_ai.ui_label) — pass-through of the search
    service's /ui-labels, which also reports which languages may be offered.

    NOT TTL-cached like the two above, and it returns the WHOLE body rather than
    one key: a curator editing a label wants to see it on the next reload, and the
    availability contract (available/default/incomplete) only makes sense whole.
    """
    try:
        resp = requests.get(f"{SEARCH_V1}/ui-labels", params={"scope": scope},
                            timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("/ui-labels fetch failed (%s)", e)
        raise HTTPException(status_code=503, detail="UI labels unavailable")


@app.get("/v1/ingest-status")
def ingest_status(processing_uuid: str | None = None,
                  since_seconds: int | None = None):
    """Per-file progress of one ingest run — pass-through of the search service's
    /ingest-status, for the upload page to poll. Never cached: the whole point is
    freshness.

    `since_seconds` picks the newest run started inside that window, because THIS
    endpoint's sibling POST /v1/ingest is synchronous — it hands back the
    processing_uuid only when the flow is already done, so a page that waited for
    it would only ever show a finished run.
    """
    params = {k: v for k, v in
              (("processing_uuid", processing_uuid),
               ("since_seconds", since_seconds)) if v is not None}
    if not params:
        raise HTTPException(status_code=400,
                            detail="pass processing_uuid or since_seconds")
    try:
        resp = requests.get(f"{SEARCH_V1}/ingest-status", params=params, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("/ingest-status fetch failed (%s)", e)
        raise HTTPException(status_code=503, detail="Ingest status unavailable")


@app.post("/v1/ingest")
async def ingest(
    file: UploadFile = File(None),
    url: str | None = Form(None),

    # WHO and FOR WHOM — all three required, all three verified against the
    # register before anything is ingested (see _validate_principal).
    legal_entity: str = Form(...),
    user_id: str = Form(...),
    visibility: str = Form(...),

    # user_name is no longer a form field either: it comes from the register (the
    # uploader's display_name), so the client cannot claim someone else's name.

    # QA packs: the pack's NAME and its REPLACEMENT KEY — uploading a pack of
    # this name replaces that school's previous version of it. REQUIRED for a QA
    # upload (validated below) and never derived: the old derivation produced
    # "cs-core" for every plain upload, so the school's second pack silently
    # replaced its first. ASCII letters, digits, '-' and '_' only, so the name
    # survives a URL, a filename and a shell without quoting.
    user_defined_pack_id: str | None = Form(None),
    # QA packs only: what an ABSENT row in the CSV means. `merge` (default) is
    # the safe one — the file says what it touches and rows it never mentions
    # stay as they are, so a mangled spreadsheet edit fails to update something
    # instead of deleting it. `replace` makes the file the whole pack.
    # A deletion is stated with is_active=FALSE on the row, in either mode.
    qa_mode: str = Form(
        "merge",
        description=("QA packs only. merge: rows the file does not mention are "
                     "left alone. replace: the file is the whole pack, anything "
                     "missing from it is deactivated. Delete one entry by "
                     "setting is_active=FALSE on its row, in either mode."),
    ),
    document_type: str | None = Form(None),
    document_language: str | None = Form(None),
    school_subject: str | None = Form(None),
    # DECLARED grades — now a SET: one document may target several grades
    # (e.g. a whole "první stupeň" 1–5). Mirrors `domains`. Each is a school_grade
    # číselník code (1…9, 9999). Single or repeated field both arrive as a list.
    school_subject_grades: list[str] = Form(default=[]),

    save_level_1_child: bool = Form(False),
    max_child_links: int = Form(MAX_CHILD_LINKS),
    audience_roles: list[str] = Form(default=[]),
    domains: list[str] = Form(default=[]),  # DECLARED taxonomy_rules DOMAIN codes; override the LLM
    # document validity (ISO dates) — an organisation period picked in the UI or
    # custom dates; absent = NULL = unlimited. Search hard-filters expired rows.
    valid_from: str | None = Form(None),
    valid_to: str | None = Form(None),
    dry_run: bool = Form(False),
):

    # Refuse before touching Langflow: an unknown entity, a user who does not
    # belong to it, or an unknown visibility is a 400, never a default.
    principal = _validate_principal(legal_entity, user_id, visibility)
    user_name = principal["user_display_name"]

    if (file is None and url is None) or (file and url):
        raise HTTPException(
            status_code=400,
            detail="Provide either file OR url"
        )

    # document_type: no longer free text — validate against the taxonomy číselník
    # (case-insensitive, coerced to the canonical type_code). Empty = the LLM
    # classifies during ingestion. If the taxonomy is unavailable (search service
    # down and no cache), accept as-is — soft dependency, never block ingestion.
    if document_type:
        types = _get_document_types()
        if types is not None:
            by_code = {t["type_code"].upper(): t["type_code"] for t in types}
            coerced = by_code.get(document_type.strip().upper())
            if coerced is None:
                raise HTTPException(
                    status_code=400,
                    detail={"error": f"Unknown document_type: {document_type!r}",
                            "allowed": sorted(by_code.values())},
                )
            document_type = coerced
        else:
            logger.warning("document_type %r accepted UNVALIDATED (taxonomy unavailable)", document_type)

    # language / subject / grade: číselník codes (edu_ai.code_list via the search
    # service) — same contract as document_type: coerce case, 400 + allowed list
    # on unknown, accept unvalidated only when the lists are unreachable.
    if document_language or school_subject or school_subject_grades:
        lists = _get_code_lists()
        if lists is not None:
            def _coerce_code(value, list_code, transform):
                allowed = {i["code"] for i in lists.get(list_code, [])}
                if not allowed:
                    return value
                cand = transform(value.strip())
                if cand not in allowed:
                    raise HTTPException(status_code=400, detail={
                        "error": f"Unknown {list_code}: {value!r}",
                        "allowed": sorted(allowed)})
                return cand
            if document_language:
                document_language = _coerce_code(document_language, "language", str.lower)
            if school_subject:
                school_subject = _coerce_code(school_subject, "school_subject", str.upper)
            if school_subject_grades:
                school_subject_grades = [_coerce_code(g, "school_grade", str.upper)
                                         for g in school_subject_grades if g and g.strip()]
        else:
            logger.warning("code lists unavailable — language/subject/grade accepted UNVALIDATED")

    # AUDIENCE ROLES (init 127): >=1, each a code_list('role') code. Hard
    # audience filter at search — a document with no role would be invisible to
    # every role-stated caller, so at least one is mandatory. Validated like the
    # other číselníky; unreachable list -> accept as-is (soft dependency).
    roles = [r.strip().upper() for r in (audience_roles or []) if r.strip()]
    # >=1 role is mandatory ONLY for entity/organization: the role is a hard
    # audience filter there, so a role-less doc would be invisible to everyone.
    # For PRIVATE the search predicate ignores role entirely (only the owner sees
    # it), so a role would never be consulted — requiring one would be noise.
    if not roles and visibility != "private":
        raise HTTPException(status_code=400, detail=(
            "audience_roles: at least one role is required for entity/organization "
            "visibility (STUDENT / TEACHER / PARENT)"))
    _lists = _get_code_lists()
    if _lists is not None:
        _allowed = {i["code"] for i in _lists.get("role", [])}
        if _allowed:
            bad = [r for r in roles if r not in _allowed]
            if bad:
                raise HTTPException(status_code=400, detail={
                    "error": f"Unknown audience_roles: {bad}",
                    "allowed": sorted(_allowed)})
    roles = sorted(set(roles))

    # DOCUMENT VALIDITY: ISO dates, from <= to when both. Search filters expired
    # rows hard (scope validity), so a typo here silently hides the document —
    # reject malformed input instead of storing it.
    from datetime import date as _date
    _vf = _vt = None
    try:
        _vf = _date.fromisoformat(valid_from.strip()) if valid_from and valid_from.strip() else None
        _vt = _date.fromisoformat(valid_to.strip()) if valid_to and valid_to.strip() else None
    except ValueError:
        raise HTTPException(status_code=400, detail=(
            "valid_from/valid_to: expected ISO dates (YYYY-MM-DD), got "
            f"{valid_from!r} / {valid_to!r}"))
    if _vf and _vt and _vf > _vt:
        raise HTTPException(status_code=400, detail=(
            f"valid_from ({_vf}) must not be after valid_to ({_vt})"))
    valid_from = _vf.isoformat() if _vf else None
    valid_to = _vt.isoformat() if _vt else None

    # DECLARED domains must belong to THIS school's resolved catalog (per-unit:
    # a ZŠ may not declare "university admissions", a SŠ may not declare
    # "secondary-school admissions", …). Validated like document_type: coerce to
    # upper, 400 + allowed list on an out-of-catalog code, accept unvalidated only
    # when the catalog is unreachable (soft dependency, never blocks ingestion).
    domains = [d.strip().upper() for d in (domains or []) if d and d.strip()]
    if domains:
        from urllib.parse import quote as _q
        _dom = _fetch_from_search(f"/domains?legal_entity={_q(legal_entity)}", "domains")
        if _dom is not None:
            _allowed_dom = {d["code"].upper() for d in _dom}
            _bad = [d for d in domains if d not in _allowed_dom]
            if _bad:
                raise HTTPException(status_code=400, detail={
                    "error": f"Domain(s) not in this school's catalog: {_bad}",
                    "legal_entity": legal_entity,
                    "allowed": sorted(_allowed_dom)})
        else:
            logger.warning("domain catalog unreachable — domains %s accepted UNVALIDATED", domains)

    processing_uuid = str(uuid.uuid4())

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Collect all files to process: [{file_uuid, file_name, file_path,
    #                                  source_url, source_type, page_role}]
    files_to_process = []

    # ── FILE UPLOAD ──────────────────────────────────────────
    if file:
        file_uuid = str(uuid.uuid4())
        safe_filename = normalize_filename(file.filename)
        ext = Path(safe_filename).suffix.lower()

        if ext not in SUPPORTED_FILE_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: {ext}. "
                       f"Supported: {', '.join(sorted(SUPPORTED_FILE_EXTENSIONS))}"
            )

        final_filename = f"{file_uuid}_{safe_filename}"
        file_path = UPLOAD_DIR / final_filename

        content = await file.read()

        if not content:
            raise HTTPException(status_code=400, detail="Empty file")

        with open(file_path, "wb") as f:
            f.write(content)

        source_type = detect_source_type(safe_filename)

        # QA packs are versioned BY NAME, so the name has to be given, not guessed
        if source_type == "qa":
            if qa_mode not in ("merge", "replace"):
                raise HTTPException(status_code=400, detail={
                    "error": f"qa_mode {qa_mode!r} is not allowed",
                    "allowed": ["merge", "replace"],
                    "why": ("merge leaves entries the file does not mention alone; "
                            "replace deactivates them. Delete a single entry by "
                            "setting is_active=FALSE on its row.")})
            _pid = (user_defined_pack_id or "").strip()
            if not _pid:
                raise HTTPException(status_code=400, detail={
                    "error": "user_defined_pack_id is required for a QA pack",
                    "why": ("the pack name is its replacement key — uploading a pack "
                            "of the same name replaces that school's previous version"),
                    "example": "prijimacky"})
            if not _PACK_ID_RX.match(_pid):
                raise HTTPException(status_code=400, detail={
                    "error": f"user_defined_pack_id {_pid!r} is not allowed",
                    "allowed": "ASCII letters, digits, '-' and '_', max 64 chars",
                    "why": "no diacritics or spaces — the name travels in URLs, filenames and shell commands",
                    "example": "prijimacky-2026"})

        files_to_process.append({
            "file_uuid": file_uuid,
            "file_name": final_filename,
            "file_path": str(file_path),
            "source_url": None,
            "source_type": source_type,
            "page_role": "upload",
        })

        if save_level_1_child:
            raise HTTPException(
                status_code=400,
                detail="save_level_1_child is only supported for URL ingestion"
            )

    # ── URL INGESTION ────────────────────────────────────────
    else:
        try:
            normalized_url = normalize_url(url)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid URL format")

        if not validators.url(normalized_url):
            raise HTTPException(status_code=400, detail="Invalid URL")

        # ── 1) Fetch once, then branch on the real content type ──
        try:
            resp = requests.get(normalized_url, timeout=60)
            resp.raise_for_status()
        except Exception:
            raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {normalized_url}")

        file_ext = resolve_file_ext(normalized_url, resp.headers.get("content-type", ""))

        if file_ext:
            # URL is a direct file (PDF/Office) — save the bytes like an upload
            # and route to the PDF/Office flow (→ markdown flow), NOT the web
            # scraper. A binary file has no HTML links, so no child crawl.
            parent_file_uuid = str(uuid.uuid4())
            base_name = url_to_safe_filename(normalized_url)
            final_filename = f"{parent_file_uuid}_{base_name}{file_ext}"
            file_path = UPLOAD_DIR / final_filename

            with open(file_path, "wb") as f:
                f.write(resp.content)

            files_to_process.append({
                "file_uuid": parent_file_uuid,
                "file_name": final_filename,
                "file_path": str(file_path),
                "source_url": normalized_url,
                "source_type": detect_source_type(final_filename),
                "page_role": "parent",
            })

        else:
            # ── HTML page: save it + optionally crawl level-1 children ──
            header_charset = None
            m = re.search(r"charset=([a-zA-Z0-9_\-]+)", resp.headers.get("content-type", ""), re.I)
            if m:
                header_charset = m.group(1)
            html = decode_html(resp.content, header_charset)

            parent_file_uuid = str(uuid.uuid4())
            base_name = url_to_safe_filename(normalized_url)
            parent_filename = f"{parent_file_uuid}_{base_name}_P.html"
            parent_file_path = UPLOAD_DIR / parent_filename

            with open(parent_file_path, "w", encoding="utf-8") as f:
                f.write(html)

            files_to_process.append({
                "file_uuid": parent_file_uuid,
                "file_name": parent_filename,
                "file_path": str(parent_file_path),
                "source_url": normalized_url,
                "source_type": "web",
                "page_role": "parent",
            })

            # ── 2) Fetch and save child pages/files ─────────────
            if save_level_1_child:
                # cap child crawl breadth; hard ceiling of 10
                child_cap = max(1, min(int(max_child_links), 10))
                child_links = extract_links(html, normalized_url, child_cap)

                saved_children = fetch_and_save_children(
                    child_links, processing_uuid
                )

                files_to_process.extend(saved_children)

    # ── PROCESS ALL FILES VIA LANGFLOW (synchronous) ─────────
    parent_file_uuid = files_to_process[0]["file_uuid"] if files_to_process else None

    results = []

    if dry_run:
        # Dry run — skip Langflow, just report what would be processed
        for file_info in files_to_process:
            results.append({
                "file_uuid": file_info["file_uuid"],
                "file_name": file_info["file_name"],
                "file_path": file_info["file_path"],
                "source_url": file_info.get("source_url"),
                "source_type": file_info["source_type"],
                "page_role": file_info["page_role"],
                "status": "dry_run",
            })
    else:
        for file_info in files_to_process:
            result = process_file(
                file_uuid=file_info["file_uuid"],
                file_name=file_info["file_name"],
                file_path=file_info["file_path"],
                source_type=file_info["source_type"],
                source_url=file_info.get("source_url"),
                page_role=file_info["page_role"],
                processing_uuid=processing_uuid,
                parent_file_uuid=parent_file_uuid if file_info["page_role"] == "child" else None,
                user_name=user_name,
                legal_entity=legal_entity,
                user_id=user_id,
                visibility=visibility,
                document_type=document_type,
                user_defined_pack_id=user_defined_pack_id,
                qa_mode=qa_mode,
                # child documents do NOT inherit the parent's language — a linked
                # page/PDF may be in a different language than the mother page;
                # each child gets its own deterministic detection in the flow
                document_language=None if file_info["page_role"] == "child" else document_language,
                school_subject=school_subject,
                school_subject_grades=school_subject_grades,
                audience_roles=roles,
                domains=[d.strip().upper() for d in (domains or []) if d.strip()],
                valid_from=valid_from,
                valid_to=valid_to,
            )
            results.append(result)

    # ── RESPONSE ─────────────────────────────────────────────
    ok_count = sum(1 for r in results if r["status"] in ("ok", "dry_run"))
    error_count = sum(1 for r in results if r["status"] == "error")

    if dry_run:
        overall_status = "dry_run"
    elif error_count == 0:
        overall_status = "completed"
    elif ok_count == 0:
        overall_status = "failed"
    else:
        overall_status = "partial"

    return {
        "processing_uuid": processing_uuid,
        "status": overall_status,
        "total_files": len(results),
        "ok_count": ok_count,
        "error_count": error_count,
        "files": results,
    }

