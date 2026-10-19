FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN useradd --uid 10001 --create-home app \
    && mkdir /data \
    && chown app:app /data

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY tickets.py ./
COPY ticket_backend ./ticket_backend
USER app

ENTRYPOINT ["python", "tickets.py", "--database", "/data/tickets.db"]
CMD ["--help"]
