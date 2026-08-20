ALTER TABLE users ADD COLUMN recovery_code_hash TEXT;
ALTER TABLE users ADD COLUMN recovery_code_created_at TEXT;

CREATE UNIQUE INDEX idx_users_recovery_code_hash
ON users(recovery_code_hash)
WHERE recovery_code_hash IS NOT NULL;

CREATE TABLE login_rate_limits (
    identifier_hash TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL CHECK (failure_count >= 1),
    window_started_at TEXT NOT NULL,
    locked_until TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_login_rate_limits_cleanup
ON login_rate_limits(updated_at);

ALTER TABLE conversations ADD COLUMN title TEXT;
ALTER TABLE conversations ADD COLUMN creation_key_hash TEXT;
ALTER TABLE conversations ADD COLUMN creation_request_hash TEXT;

CREATE UNIQUE INDEX idx_conversations_user_creation_key
ON conversations(user_id, creation_key_hash)
WHERE creation_key_hash IS NOT NULL;
