CREATE TABLE calendar_action_proposals (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('CREATE')),
    status TEXT NOT NULL CHECK (
        status IN (
            'PENDING_APPROVAL', 'EXECUTING', 'COMPLETED', 'REJECTED',
            'FAILED', 'UNKNOWN', 'CONFLICT'
        )
    ),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    payload_hash TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    decision_key_hash TEXT,
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (user_id, run_id),
    UNIQUE (user_id, provider_event_id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (run_id, user_id) REFERENCES orchestration_runs(id, user_id)
);

CREATE INDEX idx_calendar_proposals_user_status
ON calendar_action_proposals(user_id, status, updated_at);
