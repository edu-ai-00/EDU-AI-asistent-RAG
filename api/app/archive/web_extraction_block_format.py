# last change in code by Martin Safranek (2026-05-27).
# ═══════════════════════════════════════════════════════════════════
# ARCHIVED: Web page extraction — HTML → [BLOCK] format
#
# Removed from main.py on 2026-05-27 as part of the unified
# Markdown ingestion architecture refactoring.
#
# This code was the FastAPI-side web extraction pipeline:
#   1. trafilatura XML → [BLOCK] format  (primary)
#   2. BeautifulSoup → [BLOCK] format    (fallback)
#
# The [BLOCK] format included hierarchy tracking (parent_path,
# parent_header) but the downstream Langflow TxtWebToStructured
# component only used type + text fields.
#
# This logic will be reimplemented as Langflow custom components
# in Workflow 1 (format → Markdown → LLM hierarchy enrichment),
# using trafilatura's native Markdown output instead of [BLOCK].
#
# KEY FUNCTIONS:
#   html_to_structured_text()       — main entry point
#   _trafilatura_xml_to_blocks()    — primary extraction
#   _fallback_bs_extraction()       — BeautifulSoup fallback
#   _is_noise_paragraph()           — noise detection
#   _remove_noise_bs()              — BS noise removal
#   _find_content_root()            — content root detection
#
# DEPENDENCIES (no longer needed in FastAPI):
#   trafilatura, lxml.etree
#   (beautifulsoup4 and lxml are still needed for extract_links())
# ═══════════════════════════════════════════════════════════════════

import re
import logging
from bs4 import BeautifulSoup
from lxml import etree

logger = logging.getLogger(__name__)

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    logger.warning("trafilatura not installed — using BeautifulSoup fallback only")


def clean_text(text: str) -> str:
    return text.replace("​", "").strip()


# -------------------------------------------------------------------
# NOISE DETECTION
# -------------------------------------------------------------------

_COPYRIGHT_RE = re.compile(r"©|\bcopyright\b|\ball rights reserved\b", re.I)
_PAGINATION_RE = re.compile(r"^Zobrazeno\b|^Showing\b|^Page \d", re.I)
_NOISE_PARAGRAPH_TEXTS = {"Ikona", "Icon", "Zpět", "Nahoru", "Back", "Top"}

_NOISE_TOKENS = {
    "menu", "nav", "navbar", "navigation", "sidebar", "cookie",
    "consent", "breadcrumb", "footer", "banner", "toolbar",
    "social", "share", "widget", "toc", "search", "gdpr", "privacy",
}
_NOISE_SUBSTRINGS = [
    "skip-link", "cc-window", "cc-banner", "table-of-contents",
    "mobile-menu", "mega-menu", "dropdown-menu",
    "site-header", "site-footer", "site-nav",
    "top-bar", "bottom-bar", "header-nav", "search-form",
]
_NOISE_ROLES = ["navigation", "banner", "contentinfo", "search"]
_NOISE_TAGS = ["nav", "header", "footer", "aside", "form",
               "script", "style", "noscript", "iframe"]
_CAMEL_SPLIT_RE = re.compile(r'[A-Z][a-z]+|[a-z]+|[A-Z]+(?=[A-Z][a-z]|\b)')


def _is_noise_paragraph(text: str) -> bool:
    """Detect paragraphs that are noise (copyright, pagination, UI)."""
    if _COPYRIGHT_RE.search(text):
        return True
    if _PAGINATION_RE.match(text):
        return True
    if text in _NOISE_PARAGRAPH_TEXTS:
        return True
    return False


# ═══════════════════════════════════════════════════════════════════
# PRIMARY: trafilatura XML -> [BLOCK] format
# ═══════════════════════════════════════════════════════════════════

def _extract_element_text(el) -> str:
    """Extract full text from an lxml XML element, including children."""
    return clean_text("".join(el.itertext()))


def _trafilatura_xml_to_blocks(xml_str: str, source_url: str,
                               title_text: str = None) -> str:
    """Parse trafilatura XML and convert to [BLOCK] format."""
    root = etree.fromstring(xml_str.encode("utf-8"))
    main_el = root.find(".//main")
    if main_el is None:
        return ""

    output = []
    block_id = 1
    current_header = None
    header_stack = {}

    # -- Metadata --
    output.append("[DOCUMENT_START]")
    output.append(f"[SOURCE_URL] {source_url}")
    if title_text:
        output.append(f"[TITLE] {title_text}")

    # -- Process child elements of <main> --
    for el in main_el:
        tag = el.tag

        # HEADING: <head rend="h1|h2|h3|h4">
        if tag == "head":
            rend = el.get("rend", "")
            text = _extract_element_text(el)
            if not text:
                continue
            level_match = re.match(r"h(\d)", rend)
            level = level_match.group(1) if level_match else "2"

            header_stack[level] = text
            for lvl in list(header_stack.keys()):
                if int(lvl) > int(level):
                    del header_stack[lvl]
            parent_path = " > ".join(
                header_stack[l] for l in sorted(header_stack.keys(), key=int)
            )
            current_header = text

            output.append(f"\n[BLOCK]")
            output.append(f"id={block_id}")
            output.append("type=header")
            output.append(f"level={level}")
            output.append(f"text={text}")
            output.append(f"parent_path={parent_path}")
            block_id += 1

        # PARAGRAPH: <p>
        elif tag == "p":
            text = _extract_element_text(el)
            if not text or _is_noise_paragraph(text):
                continue

            output.append(f"\n[BLOCK]")
            output.append(f"id={block_id}")
            output.append("type=paragraph")
            if current_header:
                output.append(f"parent_header={current_header}")
            if header_stack:
                parent_path = " > ".join(
                    header_stack[l]
                    for l in sorted(header_stack.keys(), key=int)
                )
                output.append(f"parent_path={parent_path}")
            output.append(f"text={text}")
            block_id += 1

        # LIST: <list rend="ul|ol">
        elif tag == "list":
            rend = el.get("rend", "ul")
            list_type = "ol" if rend == "ol" else "ul"
            items = []
            for item_el in el.findall("item"):
                item_text = _extract_element_text(item_el)
                if item_text:
                    items.append(item_text)
            if items:
                output.append(f"\n[BLOCK]")
                output.append(f"id={block_id}")
                output.append("type=list")
                if current_header:
                    output.append(f"parent_header={current_header}")
                if header_stack:
                    parent_path = " > ".join(
                        header_stack[l]
                        for l in sorted(header_stack.keys(), key=int)
                    )
                    output.append(f"parent_path={parent_path}")
                output.append(f"list_type={list_type}")
                output.append("items=")
                for item in items:
                    output.append(f"- {item}")
                block_id += 1

        # TABLE: <table>
        elif tag == "table":
            rows_text = []
            for row_el in el.findall("row"):
                cells = []
                for cell_el in row_el.findall("cell"):
                    cell_text = _extract_element_text(cell_el)
                    if cell_text:
                        cells.append(cell_text)
                if cells:
                    rows_text.append(" | ".join(cells))
            if rows_text:
                output.append(f"\n[BLOCK]")
                output.append(f"id={block_id}")
                output.append("type=table")
                if current_header:
                    output.append(f"parent_header={current_header}")
                if header_stack:
                    parent_path = " > ".join(
                        header_stack[l]
                        for l in sorted(header_stack.keys(), key=int)
                    )
                    output.append(f"parent_path={parent_path}")
                table_text = "\n".join(rows_text)
                output.append(f"text={table_text}")
                block_id += 1

    return "\n".join(output)


# ═══════════════════════════════════════════════════════════════════
# FALLBACK: Enhanced BeautifulSoup extraction
# ═══════════════════════════════════════════════════════════════════

def _tokenize_css_name(name: str) -> set:
    """Split CSS class/id into tokens (handles camelCase + hyphens)."""
    parts = re.split(r'[-_\s]+', name)
    tokens = set()
    for part in parts:
        camel_parts = _CAMEL_SPLIT_RE.findall(part)
        if camel_parts:
            tokens.update(p.lower() for p in camel_parts)
        elif part:
            tokens.add(part.lower())
    return tokens


def _has_noise_class_or_id(el) -> bool:
    if el.attrs is None:
        return False
    classes = el.get("class", [])
    el_id = el.get("id") or ""
    all_tokens = set()
    for cls in classes:
        all_tokens.update(_tokenize_css_name(cls))
    all_tokens.update(_tokenize_css_name(el_id))
    if all_tokens & _NOISE_TOKENS:
        return True
    combined = (" ".join(classes) + " " + el_id).lower()
    return any(pat in combined for pat in _NOISE_SUBSTRINGS)


def _remove_noise_bs(main_el):
    """Enhanced noise removal from BeautifulSoup element."""
    to_remove = []
    to_remove.extend(main_el.find_all(_NOISE_TAGS))
    for role in _NOISE_ROLES:
        to_remove.extend(main_el.find_all(attrs={"role": role}))
    for el in main_el.find_all(True):
        if _has_noise_class_or_id(el):
            to_remove.append(el)
    to_remove.extend(main_el.find_all(attrs={"data-cookieconsent": True}))
    to_remove.extend(main_el.find_all(
        attrs={"aria-label": re.compile(r"cookie|consent|gdpr", re.I)}
    ))
    seen = set()
    for el in to_remove:
        oid = id(el)
        if oid not in seen and el.parent is not None:
            seen.add(oid)
            el.decompose()


def _find_content_root(soup):
    """Find the best content root element in HTML."""
    main = soup.find("main")
    if main:
        return main
    for attr in ["id", "class"]:
        for pattern in ["content", "main-content", "page-content",
                        "article-content", "entry-content",
                        "post-content", "main", "article"]:
            if attr == "id":
                el = soup.find(attrs={"id": re.compile(pattern, re.I)})
            else:
                el = soup.find(attrs={"class": re.compile(pattern, re.I)})
            if el and len(el.get_text(strip=True)) > 200:
                return el
    article = soup.find("article")
    if article:
        return article
    return soup.body


def _fallback_bs_extraction(html: str, source_url: str) -> str:
    """BeautifulSoup fallback when trafilatura returns nothing."""
    soup = BeautifulSoup(html, "lxml")
    content_root = _find_content_root(soup)
    if not content_root:
        return ""

    output = []
    block_id = 1
    current_header = None
    header_stack = {}

    output.append("[DOCUMENT_START]")
    output.append(f"[SOURCE_URL] {source_url}")
    title_tag = soup.find("title")
    if title_tag:
        output.append(f"[TITLE] {clean_text(title_tag.get_text())}")

    _remove_noise_bs(content_root)

    h1 = content_root.find("h1")
    if h1:
        text = clean_text(h1.get_text())
        if text:
            current_header = text
            header_stack = {"1": text}
            output.append(f"\n[BLOCK]")
            output.append(f"id={block_id}")
            output.append("type=header")
            output.append("level=1")
            output.append(f"text={text}")
            output.append(f"parent_path={text}")
            block_id += 1

    for el in content_root.find_all(["h2", "h3", "h4", "p", "ul", "ol"]):
        if el.name.startswith("h"):
            text = clean_text(el.get_text())
            if not text:
                continue
            level = el.name[1]
            header_stack[level] = text
            for lvl in list(header_stack.keys()):
                if int(lvl) > int(level):
                    del header_stack[lvl]
            parent_path = " > ".join(
                header_stack[l]
                for l in sorted(header_stack.keys(), key=int)
            )
            current_header = text
            output.append(f"\n[BLOCK]")
            output.append(f"id={block_id}")
            output.append("type=header")
            output.append(f"level={level}")
            output.append(f"text={text}")
            output.append(f"parent_path={parent_path}")
            block_id += 1

        elif el.name == "p":
            text = clean_text(el.get_text(" ", strip=True))
            if not text or _is_noise_paragraph(text):
                continue
            output.append(f"\n[BLOCK]")
            output.append(f"id={block_id}")
            output.append("type=paragraph")
            if current_header:
                output.append(f"parent_header={current_header}")
            if header_stack:
                parent_path = " > ".join(
                    header_stack[l]
                    for l in sorted(header_stack.keys(), key=int)
                )
                output.append(f"parent_path={parent_path}")
            output.append(f"text={text}")
            block_id += 1

        elif el.name in ["ul", "ol"]:
            items = []
            for li in el.find_all("li", recursive=False):
                text = clean_text(li.get_text(" ", strip=True))
                if text:
                    items.append(text)
            if items:
                output.append(f"\n[BLOCK]")
                output.append(f"id={block_id}")
                output.append("type=list")
                if current_header:
                    output.append(f"parent_header={current_header}")
                if header_stack:
                    parent_path = " > ".join(
                        header_stack[l]
                        for l in sorted(header_stack.keys(), key=int)
                    )
                    output.append(f"parent_path={parent_path}")
                output.append(
                    f"list_type={'ul' if el.name == 'ul' else 'ol'}"
                )
                output.append("items=")
                for item in items:
                    output.append(f"- {item}")
                block_id += 1

    return "\n".join(output)


# ═══════════════════════════════════════════════════════════════════
# MAIN: Hybrid extraction entry point
# ═══════════════════════════════════════════════════════════════════

_MIN_TRAFILATURA_BLOCKS = 2


def html_to_structured_text(html: str, source_url: str) -> str:
    """
    Hybrid content extraction:
    1. Primary: trafilatura XML -> [BLOCK] format
    2. Fallback: enhanced BeautifulSoup if trafilatura fails
    """
    result = ""
    method = "none"

    # -- Primary: trafilatura --
    if HAS_TRAFILATURA:
        try:
            xml = trafilatura.extract(
                html,
                output_format="xml",
                include_formatting=True,
                include_links=True,
                include_tables=True,
            )
            if xml:
                soup_title = BeautifulSoup(html, "lxml")
                title_tag = soup_title.find("title")
                title_text = (clean_text(title_tag.get_text())
                              if title_tag else None)

                result = _trafilatura_xml_to_blocks(
                    xml, source_url, title_text
                )
                if result.count("[BLOCK]") >= _MIN_TRAFILATURA_BLOCKS:
                    method = "trafilatura"
                    logger.info(
                        "html_to_structured_text: trafilatura extracted "
                        f"{result.count('[BLOCK]')} blocks from {source_url}"
                    )
        except Exception as e:
            logger.warning(
                f"trafilatura failed for {source_url}: {e}, "
                "falling back to BeautifulSoup"
            )

    # -- Fallback: BeautifulSoup --
    if method == "none":
        result = _fallback_bs_extraction(html, source_url)
        logger.info(
            "html_to_structured_text: BeautifulSoup fallback extracted "
            f"{result.count('[BLOCK]')} blocks from {source_url}"
        )

    return result
