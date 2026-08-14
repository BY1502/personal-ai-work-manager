CREATE TABLE domain_events (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT,
    source_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    occurred_at TEXT NOT NULL,
    UNIQUE (user_id, event_type, source_digest),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_domain_events_user_occurred
ON domain_events(user_id, occurred_at, id);

CREATE TABLE trigger_suggestions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    target_type TEXT,
    target_ref TEXT,
    source_digest TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'DISMISSED', 'EXPIRED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, trigger_type, source_digest, policy_version),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_trigger_suggestions_user_status
ON trigger_suggestions(user_id, status, updated_at);
