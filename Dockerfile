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

# Create data directory for persistent volume
RUN mkdir -p /data

# Run the bot
CMD ["python", "bot.py"]
