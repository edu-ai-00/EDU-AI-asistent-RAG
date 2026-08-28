# ---------------------------------------------------------
# Base image
# ---------------------------------------------------------
FROM python:3.11-slim

# ---------------------------------------------------------
# Environment settings
# ---------------------------------------------------------
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# ---------------------------------------------------------
# System dependencies
# ---------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libxml2 \
    libxslt1.1 \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------
# Working directory
# ---------------------------------------------------------
WORKDIR /app

# ---------------------------------------------------------
# Upgrade pip (important for wheels like lxml)
# ---------------------------------------------------------
RUN pip install --upgrade pip

# ---------------------------------------------------------
# Install Python dependencies
# ---------------------------------------------------------
RUN pip install \
    fastapi \
    uvicorn[standard] \
    requests \
    python-multipart \
    validators \
    beautifulsoup4 \
    lxml

# ---------------------------------------------------------
# Copy application code
# ---------------------------------------------------------
COPY app /app/app

# ---------------------------------------------------------
# Create non-root user (security best practice)
# ---------------------------------------------------------
# uid pinned: ./uploads is a bind mount shared with the Langflow container, so
# the host directory's ownership has to match. Unpinned, useradd takes the next
# free uid and a base-image bump silently moves it.
RUN useradd -m -u 1000 appuser
USER appuser

# ---------------------------------------------------------
# Expose port
# ---------------------------------------------------------
EXPOSE 8080

# ---------------------------------------------------------
# Healthcheck (more robust)
# ---------------------------------------------------------
# /health (not /docs): it reports config readiness too, and always answers 200
# so a misconfigured instance stays inspectable instead of crash-looping.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8080/health || exit 1

# ---------------------------------------------------------
# Start API
# ---------------------------------------------------------
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]