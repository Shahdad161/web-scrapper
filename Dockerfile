# syntax=docker/dockerfile:1
FROM python:3.11-slim

# System deps for Playwright Chromium
RUN apt-get update && apt-get install -y \
    wget gnupg ca-certificates fonts-liberation \
    libnss3 libxkbcommon0 libx11-xcb1 libxcb1 libxcomposite1 \
    libxcursor1 libxi6 libxdamage1 libxrandr2 libgbm1 libgtk-3-0 libasound2 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright + Chromium (and its deps)
RUN python -m playwright install --with-deps chromium

# Copy the rest of your app
COPY . .

ENV PYTHONUNBUFFERED=1
ENV PORT=8080
CMD ["python", "app.py"]
