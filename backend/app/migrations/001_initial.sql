CREATE TABLE users (
    id TEXT PRIMARY KEY,
    timezone TEXT NOT NULL,
    locale TEXT NOT NULL DEFAULT 'ko-KR',
    created_at TEXT NOT NULL
);

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (id, user_id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE UNIQUE INDEX idx_conversations_default_user
ON conversations(user_id)
WHERE is_default = 1;

CREATE TABLE chat_messages (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    server_sequence INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('USER', 'ASSISTANT')),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    client_message_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (conversation_id, server_sequence),
    UNIQUE (conversation_id, client_message_id),
    UNIQUE (id, user_id),
    FOREIGN KEY (conversation_id, user_id) REFERENCES conversations(id, user_id)
);

CREATE INDEX idx_chat_messages_conversation_sequence
ON chat_messages(conversation_id, server_sequence);

CREATE TABLE orchestration_runs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    conversation_id TEXT,
    request_message_id TEXT NOT NULL UNIQUE,
    intent TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'RECEIVED',
            'INTERPRETING',
            'PLANNED',
            'APPLYING',
            'NEEDS_CLARIFICATION',
            'COMPLETED',
            'FAILED',
            'INTERRUPTED_RETRYABLE'
        )
    ),
    structured_plan_json TEXT,
    plan_hash TEXT,
    memory_status TEXT,
    result_type TEXT,
    result_id TEXT,
    result_json TEXT,
    error_code TEXT,
    extractor_version TEXT,
    schema_version TEXT,
    safe_trace_json TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (id, user_id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (conversation_id, user_id) REFERENCES conversations(id, user_id),
    FOREIGN KEY (request_message_id, user_id) REFERENCES chat_messages(id, user_id)
);

CREATE INDEX idx_orchestration_runs_user_status
ON orchestration_runs(user_id, status);

CREATE TABLE execution_events (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    public_summary TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence),
    FOREIGN KEY (run_id, user_id) REFERENCES orchestration_runs(id, user_id)
);

CREATE INDEX idx_execution_events_run_sequence
ON execution_events(run_id, sequence);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (id, user_id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE UNIQUE INDEX idx_projects_user_active_name
ON projects(user_id, normalized_name)
WHERE archived_at IS NULL;

CREATE TABLE project_aliases (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    source_change_audit_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, normalized_alias),
    FOREIGN KEY (project_id, user_id) REFERENCES projects(id, user_id)
);

CREATE INDEX idx_project_aliases_project
ON project_aliases(project_id);

CREATE TABLE work_items (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('TODO', 'IN_PROGRESS', 'WAITING', 'BLOCKED', 'HOLD', 'DONE')
    ),
    priority TEXT NOT NULL DEFAULT 'NORMAL' CHECK (
        priority IN ('HIGH', 'NORMAL', 'LOW')
    ),
    waiting_for TEXT,
    blocked_reason TEXT,
    next_action TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    status_changed_at TEXT NOT NULL,
    last_activity_on TEXT,
    completed_at TEXT,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (id, user_id),
    CHECK (
        status <> 'WAITING'
        OR (waiting_for IS NOT NULL AND length(trim(waiting_for)) > 0)
    ),
    CHECK (
        status <> 'BLOCKED'
        OR (blocked_reason IS NOT NULL AND length(trim(blocked_reason)) > 0)
    ),
    CHECK (
        status <> 'DONE'
        OR (
            completed_at IS NOT NULL
            AND waiting_for IS NULL
            AND blocked_reason IS NULL
            AND next_action IS NULL
        )
    ),
    FOREIGN KEY (project_id, user_id) REFERENCES projects(id, user_id)
);

CREATE INDEX idx_work_items_user_project_status
ON work_items(user_id, project_id, status)
WHERE archived_at IS NULL;

CREATE INDEX idx_work_items_user_status_updated
ON work_items(user_id, status, updated_at)
WHERE archived_at IS NULL;

CREATE TABLE activities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    occurred_on_local TEXT NOT NULL,
    occurred_at_utc TEXT,
    timezone TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN (
            'WORK_PERFORMED',
            'REQUEST_SENT',
            'RESPONSE_RECEIVED',
            'DECISION',
            'NOTE'
        )
    ),
    summary TEXT NOT NULL,
    validity TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (
        validity IN ('ACTIVE', 'SUPERSEDED', 'RETRACTED')
    ),
    version INTEGER NOT NULL DEFAULT 1,
    superseded_by_activity_id TEXT,
    source_message_id TEXT NOT NULL,
    claim_sequence INTEGER NOT NULL,
    source_excerpt TEXT NOT NULL,
    source_excerpt_hash TEXT NOT NULL,
    derivation TEXT NOT NULL CHECK (
        derivation IN ('EXPLICIT', 'RULE_DERIVED', 'LLM_INFERRED')
    ),
    rule_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (source_message_id, claim_sequence),
    UNIQUE (id, user_id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (source_message_id, user_id) REFERENCES chat_messages(id, user_id),
    FOREIGN KEY (superseded_by_activity_id, user_id) REFERENCES activities(id, user_id)
);

CREATE INDEX idx_activities_user_occurred_on
ON activities(user_id, occurred_on_local, recorded_at_utc);

CREATE TABLE activity_links (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    activity_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    link_method TEXT NOT NULL CHECK (
        link_method IN ('EXPLICIT', 'AUTO', 'USER_CONFIRMED', 'CORRECTION')
    ),
    link_score REAL,
    decision_evidence_json TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1,
    supersedes_link_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (id, user_id),
    FOREIGN KEY (activity_id, user_id) REFERENCES activities(id, user_id),
    FOREIGN KEY (work_item_id, user_id) REFERENCES work_items(id, user_id),
    FOREIGN KEY (supersedes_link_id, user_id) REFERENCES activity_links(id, user_id)
);

CREATE UNIQUE INDEX idx_activity_links_one_active
ON activity_links(activity_id)
WHERE is_active = 1;

CREATE INDEX idx_activity_links_work_item_active
ON activity_links(work_item_id, is_active);

CREATE TABLE work_fact_groups (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    group_sequence INTEGER NOT NULL,
    source_message_id TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    draft_json TEXT NOT NULL,
    decision_json TEXT,
    target_project_id TEXT,
    target_work_item_id TEXT,
    entity_version INTEGER,
    focus_version INTEGER,
    state_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (
        status IN (
            'READY',
            'PENDING_CONFIRMATION',
            'APPLYING',
            'APPLIED',
            'REJECTED',
            'FAILED'
        )
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_message_id, group_sequence),
    UNIQUE (run_id, group_sequence),
    UNIQUE (id, user_id),
    FOREIGN KEY (run_id, user_id) REFERENCES orchestration_runs(id, user_id),
    FOREIGN KEY (source_message_id, user_id) REFERENCES chat_messages(id, user_id),
    FOREIGN KEY (target_project_id, user_id) REFERENCES projects(id, user_id),
    FOREIGN KEY (target_work_item_id, user_id) REFERENCES work_items(id, user_id)
);

CREATE INDEX idx_work_fact_groups_user_status
ON work_fact_groups(user_id, status, updated_at);

CREATE TABLE clarifications (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    fact_group_id TEXT NOT NULL,
    question TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    focus_version INTEGER,
    state_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (
        status IN ('OPEN', 'RESOLVED', 'CANCELLED', 'EXPIRED', 'SUPERSEDED')
    ),
    resolution_json TEXT,
    superseded_by_id TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE (id, user_id),
    FOREIGN KEY (fact_group_id, user_id) REFERENCES work_fact_groups(id, user_id),
    FOREIGN KEY (superseded_by_id, user_id) REFERENCES clarifications(id, user_id)
);

CREATE INDEX idx_clarifications_user_open
ON clarifications(user_id, status, created_at);

CREATE TABLE change_receipts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    fact_group_id TEXT NOT NULL UNIQUE,
    changed_field_mask_json TEXT NOT NULL,
    entity_versions_json TEXT NOT NULL,
    created_entity_ids_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (id, user_id),
    FOREIGN KEY (fact_group_id, user_id) REFERENCES work_fact_groups(id, user_id)
);

CREATE TABLE change_audit (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    fact_group_id TEXT NOT NULL,
    receipt_id TEXT,
    operation TEXT NOT NULL CHECK (
        operation IN (
            'CREATE_PROJECT',
            'PATCH_PROJECT',
            'CREATE_WORK_ITEM',
            'PATCH_WORK_ITEM',
            'MOVE_WORK_ITEM',
            'ADD_ACTIVITY',
            'RELINK_ACTIVITY',
            'RETRACT_ACTIVITY',
            'ARCHIVE_PROJECT',
            'ARCHIVE_WORK_ITEM'
        )
    ),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    expected_version INTEGER,
    applied_version INTEGER,
    before_json TEXT,
    after_json TEXT,
    correction_of_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (fact_group_id, user_id) REFERENCES work_fact_groups(id, user_id),
    FOREIGN KEY (receipt_id, user_id) REFERENCES change_receipts(id, user_id),
    FOREIGN KEY (correction_of_id) REFERENCES change_audit(id)
);

CREATE INDEX idx_change_audit_target
ON change_audit(user_id, target_type, target_id, created_at);

CREATE TABLE request_idempotency (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    method TEXT NOT NULL,
    route_fingerprint TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('IN_PROGRESS', 'COMPLETED', 'FAILED')),
    response_reference TEXT,
    response_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, method, route_fingerprint, idempotency_key),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE VIRTUAL TABLE work_memory_fts USING fts5(
    user_id UNINDEXED,
    project_id UNINDEXED,
    work_item_id UNINDEXED,
    project_name,
    project_aliases,
    work_item_title,
    searchable_text,
    tokenize = 'unicode61'
);
