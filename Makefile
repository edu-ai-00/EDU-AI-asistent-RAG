# EDU-AI asistent RAG module — developer targets.
# Run from the repository root: `make <target>`.

.PHONY: docs-check parity-vectors

# Validate the documentation set (links/anchors, sources paths, frontmatter).
docs-check:
	python3 docs/tools/docs_check.py

# Regenerate the CZ parity vectors from the live Python normalizer.
# Requires the edu_ai_search container to be running.
parity-vectors:
	docker exec -i edu_ai_search python - \
	  < contracts/parity/gen_vectors.py \
	  > contracts/parity/cs-normalization-vectors.json
	@echo "regenerated contracts/parity/cs-normalization-vectors.json"
