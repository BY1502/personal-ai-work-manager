ALTER TABLE orchestration_runs ADD COLUMN provider_name TEXT;
ALTER TABLE orchestration_runs ADD COLUMN model_version TEXT;
ALTER TABLE orchestration_runs ADD COLUMN prompt_version TEXT;

CREATE TABLE report_snapshots (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    source_run_id TEXT,
    report_type TEXT NOT NULL CHECK (
        report_type IN ('DAILY', 'WEEKLY', 'PROJECT', 'RANGE')
    ),
    project_id TEXT,
    period_start_local TEXT NOT NULL,
    period_end_local TEXT NOT NULL,
    timezone TEXT NOT NULL,
    as_of_utc TEXT NOT NULL,
    structured_sections_json TEXT NOT NULL,
    rendered_text TEXT NOT NULL,
    source_manifest_json TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    freshness TEXT NOT NULL DEFAULT 'FRESH' CHECK (
        freshness IN ('FRESH', 'STALE')
    ),
    generation_mode TEXT NOT NULL CHECK (
        generation_mode IN ('TEMPLATE', 'LLM', 'TEMPLATE_FALLBACK')
    ),
    policy_version TEXT NOT NULL,
    narrator_provider TEXT,
    model_version TEXT,
    prompt_version TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (id, user_id),
    UNIQUE (source_run_id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (source_run_id, user_id)
        REFERENCES orchestration_runs(id, user_id),
    FOREIGN KEY (project_id, user_id)
        REFERENCES projects(id, user_id)
);

CREATE INDEX idx_report_snapshots_user_period
ON report_snapshots(user_id, report_type, period_end_local, created_at);

CREATE INDEX idx_report_snapshots_project_period
ON report_snapshots(user_id, project_id, period_end_local)
WHERE project_id IS NOT NULL;
