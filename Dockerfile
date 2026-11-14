FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/instance /app/static/uploads \
    && chown appuser:appuser /app /app/instance /app/static /app/static/uploads

COPY --chown=appuser:appuser . .

USER appuser

EXPOSE 4500

CMD ["sh", "-c", "exec gunicorn -w 2 --threads 8 --timeout 120 -b 0.0.0.0:4500 --log-level info --access-logfile - --error-logfile - app:app"]