#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/app/langchain-academy"
MODULE="${LANGGRAPH_MODULE:-1}"

if ! [[ "$MODULE" =~ ^[1-6]$ ]]; then
  echo "LANGGRAPH_MODULE must be 1-6, got: $MODULE"
  exit 1
fi

for i in 1 2 3 4 5 6; do
  STUDIO_DIR="${REPO_DIR}/module-${i}/studio"
  if [ ! -d "${STUDIO_DIR}" ]; then continue; fi
  echo "OPENAI_API_KEY=\"${OPENAI_API_KEY:-}\"" > "${STUDIO_DIR}/.env"
  echo "LANGSMITH_API_KEY=\"${LANGSMITH_API_KEY:-}\"" >> "${STUDIO_DIR}/.env"
  echo "LANGSMITH_TRACING_V2=\"${LANGSMITH_TRACING_V2:-true}\"" >> "${STUDIO_DIR}/.env"
  echo "LANGSMITH_PROJECT=\"${LANGSMITH_PROJECT:-langchain-academy}\"" >> "${STUDIO_DIR}/.env"
  if [ "$i" -eq 4 ] && [ -n "${TAVILY_API_KEY:-}" ]; then
    echo "TAVILY_API_KEY=\"${TAVILY_API_KEY}\"" >> "${STUDIO_DIR}/.env"
  fi
done

STUDIO_TARGET="${REPO_DIR}/module-${MODULE}/studio"
if [ ! -d "${STUDIO_TARGET}" ]; then
  echo "Studio directory not found: ${STUDIO_TARGET}"
  exit 1
fi

cd "${STUDIO_TARGET}"
exec langgraph dev --host 0.0.0.0 --port 2024
