# last change in code by Jan Drozd (2026-09-06).
"""The chosen thematic AREAS: normalise the codes, validate them, resolve the ids.

Three operations that look like one and are not (spec.search-domain-scope §4):

  normalise   what the client sent -> the canonical set. Trim, upper-case,
              deduplicate, sort. A request is a set of areas, so order and
              repetition carry no meaning and must not reach the request hash
              as if they did.
  validate    are these codes in THIS SCHOOL's catalog
              (edu_ai.resolved_taxonomy_rules(entity, 'DOMAIN'))? A code that
              exists somewhere else in the database is not a code this caller
              may name — nearest-owner-exclusive is the whole rule of that
              function, and legalising an unknown school domain because a
              sibling school defines it would leak the sibling's taxonomy.
  resolve     which active taxonomy_rules ROW IDS carry those codes, for owners
              inside the caller's organisation or the GLOBAL owner.

The last one is the step that is easy to get wrong by leaving it out. One code
has SEVERAL ids: measured on production, SECONDARY_ADMISSIONS is id 13 in the
ZŠ's own catalog and id 42 as the global row, and five active chunks of the
organisation are labelled {42, 24}. A parent asking in that area with only id 13
in hand would lose all five. So validation happens against the school's catalog
(one code, one answer) and filtering happens against every id of that code the
organisation can legitimately have labelled its own material with.

Widening the ID set grants nothing. The rows those ids sit on still pass
scope.where() — entity, visibility, role, owner, validity — and the active
document-version join. This module decides which AREAS were asked for; it never
decides who may see a row.

Failure is NOT silent. An unreachable catalog while an EXPLICIT filter is being
validated raises DomainCatalogUnavailable, which the API turns into a retryable
503 before any retrieval and before the SSE stream opens. Answering without the
filter would be a search the user did not ask for, presented as the one they did.
"""

from __future__ import annotations

import logging

from . import config
from .db import get_cursor

logger = logging.getLogger(__name__)

# The request field these errors are about. Named once: the API builds its
# `fields` envelope from it and the tests assert on it.
FIELD = "domains"


class DomainValidationError(Exception):
    """The request's `domains` cannot be honoured as written — a 422.

    `fields` is the API envelope's list, already pointing at `domains` or at the
    offending element (`domains[1]`), because "which one of the three" is the
    only thing the client can act on.
    """

    def __init__(self, message: str, fields: list[dict]):
        self.message = message
        self.fields = fields
        super().__init__(message)


class DomainCatalogUnavailable(Exception):
    """The catalog could not be read while validating an EXPLICIT filter — a 503.

    Deliberately not a fall-through to "no restriction": the caller asked for a
    narrower search, and a wider one is a different answer, not a degraded one.
    """


def normalize(raw) -> list[str] | None:
    """The canonical code set, or None for an unrestricted search.

    Raises DomainValidationError for the shapes §3 of the spec rejects: `[]`, an
    empty or whitespace-only element, and anything that is not a list of
    strings. Membership is NOT checked here — that needs the entity, and this
    function is also what the request-hash canonicalisation uses.

    `[]` is an error and not "everything". A client that computed an empty
    selection has a bug in its own UI state, and answering it with the whole
    corpus is the one outcome that hides the bug while looking like success.
    """
    if raw is None:
        return None
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        # a bare string is the most likely client mistake and would otherwise
        # be iterated character by character
        raise DomainValidationError(
            "domains must be an array of domain codes, or null for no restriction",
            [{"field": FIELD,
              "message": "expected an array of domain codes, or null"}])
    if len(raw) == 0:
        raise DomainValidationError(
            "domains must not be empty — omit the field (or send null) to search "
            "every available area",
            [{"field": FIELD,
              "message": "must not be empty; omit the field or send null to "
                         "search every available area"}])
    out: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            raise DomainValidationError(
                f"domains[{i}] must be a domain code (string)",
                [{"field": f"{FIELD}[{i}]",
                  "message": "expected a domain code (string)"}])
        code = item.strip().upper()
        if not code:
            # NOT dropped quietly. An empty element is a client that built its
            # array from an unset variable, and silently shrinking the set would
            # answer in areas it never chose.
            raise DomainValidationError(
                f"domains[{i}] is empty",
                [{"field": f"{FIELD}[{i}]", "message": "must not be empty"}])
        out.add(code)
    return sorted(out)


def catalog(entity: str | None) -> set[str]:
    """The DOMAIN codes `entity` may name — the same set GET /v1/domains returns.

    resolved_taxonomy_rules is nearest-owner-EXCLUSIVE: a school with its own
    catalog sees only its own, otherwise it inherits the global one. That is why
    this is the authority for membership and a hand-written list never can be.
    """
    with get_cursor() as cur:
        cur.execute(
            f"SELECT type_code FROM {config.PG_SCHEMA}.resolved_taxonomy_rules(%s, 'DOMAIN')",
            (entity,))
        return {r[0] for r in cur.fetchall()}


def resolve(entity: str | None, codes: list[str] | None,
            org_entities: list[str] | None) -> tuple[list[str] | None, list[int] | None]:
    """(canonical codes, taxonomy_rules ids) for the request — (None, None) unrestricted.

    Raises DomainValidationError when a code is not in `entity`'s catalog, and
    DomainCatalogUnavailable when the lookup itself fails. ONE round trip for
    both halves: the codes are checked against the resolved catalog and expanded
    to ids in the same statement, so a catalog edit cannot land between the two.

    THE WHOLE REQUEST FAILS on one bad code (spec §3). Dropping the unknown one
    and searching the rest would answer a question about three areas with two
    and say nothing about it.
    """
    if codes is None:
        return None, None
    if not codes:
        # normalize() rejects this shape, so reaching here means a caller built
        # the list itself; treat it as the error it is rather than as "all".
        raise DomainValidationError(
            "domains must not be empty",
            [{"field": FIELD, "message": "must not be empty"}])
    orgs = list(org_entities or [])
    try:
        with get_cursor() as cur:
            # The caller's own catalog decides MEMBERSHIP…
            cur.execute(
                f"SELECT type_code FROM {config.PG_SCHEMA}.resolved_taxonomy_rules(%s, 'DOMAIN') "
                "WHERE type_code = ANY(%s)", (entity, codes))
            known = {r[0] for r in cur.fetchall()}
            unknown = [c for c in codes if c not in known]
            if unknown:
                # The codes are reference data (GET /v1/domains), so naming them
                # back is not a probe: the client got them from that endpoint and
                # a typo is what this message is for. Which ones is the actionable
                # part — "one of your three is wrong" is not.
                raise DomainValidationError(
                    "domains: unknown or inactive domain code(s) "
                    + ", ".join(repr(c) for c in unknown),
                    [{"field": f"{FIELD}[{codes.index(c)}]",
                      "message": f"unknown or inactive domain code {c!r} for "
                                 f"this school; see GET /v1/domains"}
                     for c in unknown])

            # …and the organisation's reach decides which IDS of those codes may
            # label material. Own units and the GLOBAL (NULL-owner) rows only:
            # `legal_entity_code IS NULL OR = ANY(orgs)` never crosses to another
            # legal person, so the widening cannot reach a foreign tenant's
            # taxonomy even when it happens to use the same code.
            cur.execute(
                f"SELECT id FROM {config.PG_SCHEMA}.taxonomy_rules "
                "WHERE taxonomy_type_code = 'DOMAIN' AND is_active "
                "  AND type_code = ANY(%s) "
                "  AND (legal_entity_code IS NULL OR legal_entity_code = ANY(%s))",
                (codes, orgs))
            ids = sorted(int(r[0]) for r in cur.fetchall())
    except DomainValidationError:
        raise
    except Exception as e:
        # A validated narrow search cannot degrade into a wide one. 503, and the
        # caller retries — see DomainCatalogUnavailable.
        logger.warning("domain_scope: catalog lookup failed (%s) — request refused", e)
        raise DomainCatalogUnavailable(str(e)) from e

    # An empty id list is a LEGITIMATE result, not an error: a school may hold a
    # catalog entry nothing is labelled with yet. It becomes `AND false` in
    # scope.domain_where and the search honestly finds nothing — which is what
    # spec §3 promises for "a valid code with no matching content".
    if not ids:
        logger.info("domain_scope: %s resolved to no active id for %s — "
                    "the filter admits nothing", codes, entity)
    return codes, ids
