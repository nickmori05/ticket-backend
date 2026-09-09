FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN useradd --uid 10001 --create-home app \
    && mkdir /data \
    && chown app:app /data

COPY db.py functions.py tickets.py schema.sql ./
USER app

ENTRYPOINT ["python", "tickets.py", "--database", "/data/tickets.db"]
CMD ["--help"]
