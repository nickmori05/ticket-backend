CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    email TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ticket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    from_status TEXT,
    to_status TEXT NOT NULL CHECK(to_status IN ('open', 'in_progress', 'closed')),
    note TEXT NOT NULL DEFAULT '',
    author TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ticket_events_ticket_id ON ticket_events(ticket_id, id);
CREATE INDEX IF NOT EXISTS tickets_status_id ON tickets(status, id);

-- Keys saved before keys were scoped to a caller. Read only; see submissions.py for who may replay each one.
CREATE TABLE IF NOT EXISTS ticket_submissions (
    idempotency_key TEXT PRIMARY KEY NOT NULL,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    response_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_submission_keys (
    -- The token name, or '' for submissions without an author, such as those from the terminal.
    owner TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    response_json TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (owner, idempotency_key)
);

CREATE TABLE IF NOT EXISTS ticket_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    body TEXT NOT NULL,
    author TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ticket_comments_ticket_id ON ticket_comments(ticket_id, id);

CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,
    -- Space-separated scope names; NULL for tokens created before scopes existed, which keep full access.
    scopes TEXT,
    -- Requests per minute; NULL uses the server's TICKETS_RATE_LIMIT, and 0 means no limit.
    rate_limit INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    revoked_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ticket_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    event_id INTEGER NOT NULL UNIQUE REFERENCES ticket_events(id),
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ticket_notifications_due ON ticket_notifications(status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS ticket_notifications_ticket_id ON ticket_notifications(ticket_id, id);

-- One row per API request made with a valid token. Token names are unique and never reused, so they identify
-- the caller for good. ticket_id has no foreign key because requests for missing tickets are recorded too.
CREATE TABLE IF NOT EXISTS api_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    route TEXT,
    ticket_id INTEGER,
    status INTEGER NOT NULL,
    client TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS api_audit_log_token ON api_audit_log(token, id);
CREATE INDEX IF NOT EXISTS api_audit_log_ticket_id ON api_audit_log(ticket_id, id);
