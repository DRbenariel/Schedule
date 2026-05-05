FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl-dev libffi-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DB_PATH=/data/family_calendar.db \
    PYTHONUNBUFFERED=1 \
    PORT=8080

RUN mkdir -p /data

EXPOSE 8080

CMD ["python", "main.py"]
