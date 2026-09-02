# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Containerization)
# Role: Unified Docker build for all cluster nodes.

FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    git \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium only (to keep image size lean)
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy project files
COPY . .

# Create necessary directories
RUN mkdir -p exports/evidence logs

# Create non-root user for security
RUN useradd -m -s /bin/bash spacescraper && \
    chown -R spacescraper:spacescraper /app /ms-playwright && \
    chmod -R 755 /app

# Switch to non-root user
USER spacescraper

# Expose API port
EXPOSE 8000

# Entrypoint will be overridden by docker-compose for each role
CMD ["python", "spacescraper.py"]
