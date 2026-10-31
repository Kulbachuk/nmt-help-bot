FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Тут зберігатимуться SQLite-база та логи (підключається як volume)
RUN mkdir -p /app/storage

CMD ["python", "main.py"]
