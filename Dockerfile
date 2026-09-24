FROM python:3.11-slim-bookworm

# Pinned to bookworm (not just "slim", which now resolves to trixie):
# WeasyPrint's PDF rendering needs these system libraries, and trixie
# renamed libgdk-pixbuf2.0-0. See
# https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#debian-ubuntu
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV PYTHONPATH=/app/src
ENV GAA_HOST=0.0.0.0
EXPOSE 8000

# The host sets $PORT at runtime (Render, and most PaaS hosts, do); default
# to 8000 for `docker run` without one.
CMD ["sh", "-c", "GAA_PORT=${PORT:-8000} python3 -m github_account_analysis.app"]
