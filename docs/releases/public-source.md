# Public source and scope of verification

The original publication took place on 2026-10-11; the updated upstream after the audit of 2026-10-12:
`64290d53317753dd1d4adbcc3de01527036cbf06`. The public Git has its own SHA; for installation
use the actual commit of this repository from `git rev-parse HEAD`.

The runtime code, configuration and roles, the clean DB seed, the flows, the contracts, the normalisation,
the models and the dependencies are taken over from the stated upstream. The public copy generalises
one network label and one lab docstring, not the algorithms. Internal inventories,
operational reports and the original eval questions from correspondence are not in the public tree.
The test harnesses remain; the public questions are synthetic examples that are not
compared with the historical private benchmark. The license and the attribution notices were kept.

## Documented actual VM run

| Check | Original source e40a1c0 | Fixed source 06de66d |
|---|---|---|
| Installation, 9 services, roles MATCH | PASS, cold build | PASS, build cache |
| Smoke | 26 PASS / 0 FAIL / 3 SKIP | 26 / 0 / 3 |
| Browser: school, reload, change, invalid stored choice | PASS | PASS |
| QA exact without a provider key and isolation of another school | PASS | PASS |
| Authenticated Langflow SQLite export/FTS and fixture cleanup | PASS | PASS |
| Repetition of the same wizard | FAIL, RESUME_STACK_OTHER_LAYOUT | PASS, preserved keys, flows, images and containers |

These short SHAs identify the upstream evidence; they are not a checkout instruction for this
public repo. 54 original evidence files were checked by SHA-256; the protected
operational evidence is kept by the administrator outside the public Git. The first FAIL is not relabelled as PASS.
The second browser test had a separate origin and localStorage, not a separate profile.
The fixed run did not go through a fully cold build. Firewall/isolation, paid LLM/RAG/ingest,
Cloudflare and a new VM run of the final combined public SHA are NOT_RUN.

## Final integration

The upstream combines a fix of the preflight summary, an exact check of the copied inline
Compose configs and the rejection of a compressed tar in `docker cp`. The shared wizard tests
64/64, further installation offline suites, BFF 57/57 and the actual SQLite export 25/25 passed.
The public checkout has its own check of the totals, the documentation and the installation preflight.
This offline check is not a new VM PASS nor an approval of a production upgrade.

The historical `source.base_commit` in B216 remains the original informational provenance.
Its presence in Git is explicitly skipped if a clean public or shallow checkout does not contain
the historical object; errors of Git itself are not skipped. The tests of hashes,
SQL completeness and the canonical manifest remain mandatory. The generated database
materials and the parity vectors are byte-identical with the upstream.

The public history continues from the original two commits and contains their historical
QA paths, even though the current tree has no `QA/`. Private Git objects were not transferred.
See [RELEASING.md](../RELEASING.md), the [QA/offline audit](../integrations/offline-qa-rules.md),
the [clean installation](../CLEAN_INSTALL.md) and the [Markdown index](../README.md).


## Semantic audit follow-up (2026-10-12)

The documentation audit corrects obsolete CI/CD/flow-import descriptions and
preserves the production visibility model: entity=owner entity, organization=tenant,
private=verified owner. Only log output in resolve_context was changed; its control
flow and return values match the previous source. Ten privacy cases pass and detect
the original ten content leaks; normalisation parity 30 and existing context/query-log
tests pass. The fixed isolated migration fixture passed all 47 runner scenarios across
its primary run and the second independent-start check, plus 15 offline owner guards.
This is synthetic lab validation, not production migration support or a new VM install.
Legacy flat eval/resilience commands remain unsupported; QA OpenAPI coverage and
mobile offline integration gaps are explicit. Historical public QA paths remain in
old ancestors; no private source Git history is transferred.
