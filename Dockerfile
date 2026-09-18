# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1: build the frontend
#
# A separate stage so Node never reaches the runtime image.
#
# The site is *built* here rather than committed. The build output is hashed
# per-chunk, so committing it would churn two megabytes of generated files on
# every rebuild and make every frontend diff unreviewable. The cost is that
# this stage is now load-bearing: unlike the previous optional intro bundle,
# there is no CSS fallback, so a failure here must fail the image rather than
# produce one that serves no UI.
#
# The Validation and Models pages import evaluation artifacts at build time,
# which is what stops them drifting from the committed results -- so those
# reports have to be present in this stage, not just the runtime one.
# ---------------------------------------------------------------------------
FROM node:22-slim AS frontend

WORKDIR /build/frontend/site

# Dependencies first, so a source edit does not invalidate the npm layer.
COPY frontend/site/package.json frontend/site/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/site/ ./
COPY evals/reports/ /build/evals/reports/
COPY ml/reports/ /build/ml/reports/
COPY ml/artifacts/decomposition.manifest.json /build/ml/artifacts/

RUN npm run build && npm run budget

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
# physics for the pvlib engine, firestore for the persistent cache tier and
# the shared call budget. Without the firestore extra the runbook's
# SOLARIS_CACHE_BACKEND=firestore would degrade to memory-only -- reported
# through /api/config, but still not what the deploy asked for, and the shared
# budget would quietly become per-instance.
RUN pip install --no-cache-dir ".[physics,firestore]"

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

# The built site. Vite writes into the package's static directory, so this
# lands exactly where the app's StaticFiles mount expects it.
COPY --from=frontend --chown=solaris:solaris /build/src/solaris/api/static/ ./src/solaris/api/static/

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
