CREATE TABLE validation_regression_fixtures (
    user_id TEXT NOT NULL,
    case_id TEXT NOT NULL CHECK (
        length(case_id) = 64 AND case_id NOT GLOB '*[^0-9a-f]*'
    ),
    fixture_json TEXT NOT NULL CHECK (json_valid(fixture_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, case_id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE validation_findings (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    category TEXT NOT NULL CHECK (
        category IN (
            'PROJECT_MISCLASSIFICATION',
            'DUPLICATE_WORK_ITEM',
            'DUPLICATE_ACTIVITY',
            'STATUS_INCORRECT',
            'NEXT_ACTION_INCORRECT',
            'REPORT_CORRECTION_REQUIRED',
            'CONTEXT_LINK_INCORRECT',
            'EXTRACTION_FAILURE',
            'PROVIDER_TIMEOUT',
            'MEMORY_RECOVERY',
            'SQLITE_RECOVERY',
            'OTHER'
        )
    ),
    source_type TEXT NOT NULL CHECK (
        source_type IN (
            'USER_CORRECTION',
            'OPERATOR_REVIEW',
            'AUDIT_FINDING',
            'CONTEXT_CORRECTION_EVENT'
        )
    ),
    source_ref_hash TEXT NOT NULL CHECK (
        length(source_ref_hash) = 64
        AND source_ref_hash NOT GLOB '*[^0-9a-f]*'
    ),
    recorded_at TEXT NOT NULL,
    UNIQUE (user_id, category, source_type, source_ref_hash),
    FOREIGN KEY (user_id, case_id)
        REFERENCES validation_regression_fixtures(user_id, case_id)
);

CREATE INDEX idx_validation_findings_user_recorded
ON validation_findings(user_id, recorded_at, category);

CREATE INDEX idx_validation_findings_user_case
ON validation_findings(user_id, case_id);
