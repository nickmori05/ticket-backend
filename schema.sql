CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ticket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    from_status TEXT,
    to_status TEXT NOT NULL CHECK(to_status IN ('open', 'in_progress', 'closed')),
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ticket_events_ticket_id ON ticket_events(ticket_id, id);
CREATE INDEX IF NOT EXISTS tickets_status_id ON tickets(status, id);

CREATE TABLE IF NOT EXISTS ticket_submissions (
    idempotency_key TEXT PRIMARY KEY NOT NULL,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    response_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    body TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ticket_comments_ticket_id ON ticket_comments(ticket_id, id);
