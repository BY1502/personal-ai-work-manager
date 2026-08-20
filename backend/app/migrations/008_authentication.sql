ALTER TABLE users ADD COLUMN username TEXT;
ALTER TABLE users ADD COLUMN normalized_username TEXT;
ALTER TABLE users ADD COLUMN display_name TEXT;
ALTER TABLE users ADD COLUMN password_credential TEXT;
ALTER TABLE users ADD COLUMN is_owner INTEGER NOT NULL DEFAULT 0
    CHECK (is_owner IN (0, 1));

CREATE UNIQUE INDEX idx_users_normalized_username
ON users(normalized_username)
WHERE normalized_username IS NOT NULL;

CREATE UNIQUE INDEX idx_users_single_owner
ON users(is_owner)
WHERE is_owner = 1;

CREATE TABLE auth_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    user_agent_hash TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_auth_sessions_user_active
ON auth_sessions(user_id, expires_at)
WHERE revoked_at IS NULL;
