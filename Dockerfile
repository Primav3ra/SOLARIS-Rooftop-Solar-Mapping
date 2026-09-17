# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1: build the frontend bundle
#
# Separate stage so Node never reaches the runtime image. The dashboard
# degrades to a CSS-only header if the bundle is absent, so this stage failing
# is non-fatal for the API.
# ---------------------------------------------------------------------------
FROM node:22-slim AS frontend

WORKDIR /build
COPY frontend/intro/package.json frontend/intro/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/intro/ ./
COPY src/solaris/api/static/ /out/static/
RUN npm run build || echo "frontend build failed; the CSS fallback will be used"

# ---------------------------------------------------------------------------
# Stage 2: python dependencies
#
# Installed into a virtualenv that is copied wholesale into the runtime image,
# so no build toolchain ships to production.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS deps

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only what the build backend needs, so a source edit does not invalidate
# the dependency layer.
COPY pyproject.toml README.md LICENSE ./
COPY src/solaris/__init__.py src/solaris/__init__.py
RUN pip install --no-cache-dir ".[physics]"

# ---------------------------------------------------------------------------
# Stage 3: runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    SOLARIS_ENV=prod

# Non-root. Cloud Run does not require it, but a container that never needs
# root should not run as it.
RUN useradd --create-home --uid 10001 solaris

WORKDIR /app
COPY --from=deps /opt/venv /opt/venv
COPY --chown=solaris:solaris src/ ./src/
COPY --chown=solaris:solaris pyproject.toml README.md LICENSE ./

# The built bundle, if stage 1 produced one.
COPY --from=frontend --chown=solaris:solaris /out/static/ ./src/solaris/api/static/

# Committed reference data, so the eval harness runs inside the container.
COPY --chown=solaris:solaris evals/references/ ./evals/references/

RUN pip install --no-cache-dir --no-deps -e . && chown -R solaris:solaris /app

USER solaris

# Cloud Run injects PORT; 8080 is its default.
ENV PORT=8080
EXPOSE 8080

# Liveness only -- /api/health touches nothing. /api/ready establishes the
# Earth Engine session and is what the post-deploy smoke test uses.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/api/health', timeout=4)"

CMD ["solaris-api"]
