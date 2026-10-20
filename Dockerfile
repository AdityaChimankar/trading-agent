# Trading Agent - Multi-stage Dockerfile
# Stage 1: Build Python dependencies
FROM python:3.11-slim AS builder

# Build dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cache dependencies independently of source and documentation changes.
COPY pyproject.toml ./
RUN python -c "import subprocess, tomllib; deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; subprocess.check_call(['pip', 'install', '--no-cache-dir', '--prefix=/install', *deps])"

# Install the package/console entry points without re-resolving dependencies.
FROM builder AS application
COPY README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps --prefix=/install .

# Stage 2: Playwright browser installation
FROM builder AS playwright

# Playwright system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
# Use exactly the Playwright package installed for the application.
RUN PYTHONPATH=/install/lib/python3.11/site-packages python -m playwright install chromium

# Stage 3: Final runtime image
FROM python:3.11-slim AS runtime

# Runtime dependencies only (no build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 libgomp1 \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -u 1000 trader

WORKDIR /app

# Copy installed packages from builder stage
COPY --from=application /install /usr/local

# Copy Playwright browsers from playwright stage
COPY --from=playwright /opt/playwright /opt/playwright

# Copy application code
COPY --chown=trader:trader src/ ./src/
COPY --chown=trader:trader pyproject.toml README.md ./
COPY --chown=trader:trader symbols*.txt ./
COPY --chown=trader:trader alembic.ini ./
COPY --chown=trader:trader migrations/ ./migrations/

# Default standalone runs also need writable runtime directories (no bind mounts).
RUN mkdir -p /app/data /app/models /app/.cache && \
    chown trader:trader /app/data /app/models /app/.cache

# Environment
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER trader

# Default command
CMD ["trading-agent", "--help"]
