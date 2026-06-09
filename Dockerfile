# ---------------------------------------------------------------------------
# Stage 1 — build the Go replay parser binary
# ---------------------------------------------------------------------------
FROM golang:1.23-alpine AS go-builder

WORKDIR /parser
COPY parser/ .

# go mod tidy fetches manta and generates go.sum; then we compile a static binary
RUN apk add --no-cache git \
 && go get github.com/dotabuff/manta@master \
 && go mod tidy \
 && CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o ad-parser .

# ---------------------------------------------------------------------------
# Stage 2 — the Python bot
# ---------------------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

# Install fonts for PIL image generation
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Drop the compiled Go parser into a standard location on PATH
COPY --from=go-builder /parser/ad-parser /usr/local/bin/ad-parser

# Create data directory for persistent volume
RUN mkdir -p /data

# Run the bot
CMD ["python", "bot.py"]
