FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PANGANCAST_MODEL_DIR=/data/models

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /data/models /app/data

EXPOSE 8080

CMD ["gunicorn", "app:app", "-c", "gunicorn.conf.py"]
