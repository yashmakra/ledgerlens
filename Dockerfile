FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
COPY start_render.py ./start_render.py
RUN pip install --no-cache-dir .

RUN mkdir -p /app/data/documents
EXPOSE 8000

