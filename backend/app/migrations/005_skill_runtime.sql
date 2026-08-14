CREATE TABLE skill_registry (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('DISCOVERED', 'DISABLED', 'ENABLED', 'REJECTED')
    ),
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
    content_hash TEXT NOT NULL,
    validation_errors_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(validation_errors_json)),
    discovered_at TEXT NOT NULL,
    validated_at TEXT,
    enabled_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (name, version, content_hash)
);

CREATE UNIQUE INDEX idx_skill_registry_active_name
ON skill_registry(name)
WHERE state = 'ENABLED';

CREATE TABLE skill_executions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    step_key TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    model_profile TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('PENDING', 'RUNNING', 'WAITING_TOOL', 'WAITING_SKILL',
                  'WAITING_USER', 'COMPLETED', 'FAILED', 'INTERRUPTED')
    ),
    iteration INTEGER NOT NULL DEFAULT 0 CHECK (iteration >= 0),
    max_iterations INTEGER NOT NULL CHECK (max_iterations >= 1),
    depends_on_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(depends_on_json)),
    input_digest TEXT NOT NULL,
    context_digest TEXT NOT NULL,
    output_json TEXT,
    output_digest TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (run_id, step_key),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (run_id, user_id) REFERENCES orchestration_runs(id, user_id)
);

CREATE INDEX idx_skill_executions_user_state
ON skill_executions(user_id, state, updated_at);

CREATE INDEX idx_skill_executions_run
ON skill_executions(run_id, step_key);
