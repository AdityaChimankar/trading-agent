# Trading Agent Dockerfile
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright dependencies for auto-login
RUN pip install --no-cache-dir playwright && \
    playwright install-deps chromium && \
    playwright install chromium

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install Python dependencies
RUN pip install --no-cache-dir -e ".[dev]"

# Create non-root user
RUN useradd -m -u 1000 trader && \
    chown -R trader:trader /app

USER trader

# Default command
CMD ["trading-agent", "--help"]