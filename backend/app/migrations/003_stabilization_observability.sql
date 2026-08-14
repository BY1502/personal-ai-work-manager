ALTER TABLE report_snapshots
ADD COLUMN generation_diagnostic TEXT NOT NULL DEFAULT 'NONE' CHECK (
    generation_diagnostic IN (
        'NONE',
        'NARRATOR_TIMEOUT',
        'NARRATOR_UNAVAILABLE',
        'NARRATOR_OUTPUT_REJECTED',
        'NARRATOR_FAILED'
    )
);

ALTER TABLE report_snapshots
ADD COLUMN narration_duration_ms INTEGER CHECK (
    narration_duration_ms IS NULL OR narration_duration_ms >= 0
);

ALTER TABLE report_snapshots
ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '{}';
