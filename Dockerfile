FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CREDIT_DB_PATH=/data/credit.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py .

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 双进程即可覆盖演示；线程内处理 SQLite 短事务
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "60", "wsgi:app"]
