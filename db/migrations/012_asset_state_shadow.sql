-- G3 shadow asset-state projection; existing health/API readers remain authoritative.
-- Additive and idempotent. No historical facts are backfilled or modified.
BEGIN;

CREATE TABLE IF NOT EXISTS asset_state_current (
    asset_id BIGINT PRIMARY KEY REFERENCES assets(asset_id) ON DELETE RESTRICT,
    operational_state TEXT NOT NULL,
    availability TEXT NOT NULL,
    communication_status TEXT NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL,
    state_since TIMESTAMPTZ NOT NULL,
    reason_code TEXT NOT NULL,
    reason_message TEXT NOT NULL,
    evidence_summary JSONB NOT NULL,
    evidence_schema_version INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT asset_state_current_operational_check
        CHECK (operational_state IN ('NORMAL','WARNING','CRITICAL','UNKNOWN')),
    CONSTRAINT asset_state_current_availability_check
        CHECK (availability IN ('AVAILABLE','UNAVAILABLE','UNKNOWN')),
    CONSTRAINT asset_state_current_communication_check
        CHECK (communication_status IN ('COMMUNICATING','DEGRADED','SILENT','UNKNOWN')),
    CONSTRAINT asset_state_current_evidence_check
        CHECK (jsonb_typeof(evidence_summary)='object'),
    CONSTRAINT asset_state_current_schema_version_check
        CHECK (evidence_schema_version > 0)
);

CREATE INDEX IF NOT EXISTS idx_asset_state_current_evaluated
    ON asset_state_current(evaluated_at DESC);
CREATE INDEX IF NOT EXISTS idx_asset_state_current_semantics
    ON asset_state_current(operational_state,availability,communication_status);

CREATE TABLE IF NOT EXISTS asset_state_history (
    asset_state_history_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id BIGINT NOT NULL REFERENCES assets(asset_id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL,
    from_operational_state TEXT,
    to_operational_state TEXT NOT NULL,
    from_availability TEXT,
    to_availability TEXT NOT NULL,
    from_communication_status TEXT,
    to_communication_status TEXT NOT NULL,
    state_since TIMESTAMPTZ NOT NULL,
    reason_code TEXT NOT NULL,
    reason_message TEXT NOT NULL,
    evidence_summary JSONB NOT NULL,
    evidence_schema_version INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT asset_state_history_kind_check CHECK (kind IN ('BASELINE','TRANSITION')),
    CONSTRAINT asset_state_history_from_operational_check
        CHECK (from_operational_state IS NULL OR from_operational_state IN ('NORMAL','WARNING','CRITICAL','UNKNOWN')),
    CONSTRAINT asset_state_history_to_operational_check
        CHECK (to_operational_state IN ('NORMAL','WARNING','CRITICAL','UNKNOWN')),
    CONSTRAINT asset_state_history_from_availability_check
        CHECK (from_availability IS NULL OR from_availability IN ('AVAILABLE','UNAVAILABLE','UNKNOWN')),
    CONSTRAINT asset_state_history_to_availability_check
        CHECK (to_availability IN ('AVAILABLE','UNAVAILABLE','UNKNOWN')),
    CONSTRAINT asset_state_history_from_communication_check
        CHECK (from_communication_status IS NULL OR from_communication_status IN ('COMMUNICATING','DEGRADED','SILENT','UNKNOWN')),
    CONSTRAINT asset_state_history_to_communication_check
        CHECK (to_communication_status IN ('COMMUNICATING','DEGRADED','SILENT','UNKNOWN')),
    CONSTRAINT asset_state_history_evidence_check CHECK (jsonb_typeof(evidence_summary)='object'),
    CONSTRAINT asset_state_history_schema_version_check CHECK (evidence_schema_version > 0),
    CONSTRAINT asset_state_history_baseline_check CHECK (
        (kind='BASELINE' AND from_operational_state IS NULL AND from_availability IS NULL
         AND from_communication_status IS NULL)
        OR (kind='TRANSITION' AND from_operational_state IS NOT NULL AND from_availability IS NOT NULL
            AND from_communication_status IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_asset_state_history_asset_time
    ON asset_state_history(asset_id,evaluated_at DESC,asset_state_history_id DESC);

CREATE OR REPLACE FUNCTION reject_asset_state_history_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'asset_state_history is append-only';
END;
$$ LANGUAGE plpgsql;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_asset_state_history_immutable') THEN
        CREATE TRIGGER trg_asset_state_history_immutable
            BEFORE UPDATE OR DELETE ON asset_state_history
            FOR EACH ROW EXECUTE FUNCTION reject_asset_state_history_mutation();
    END IF;
END $$;

COMMENT ON TABLE asset_state_current IS
    'G3 shadow projection. Existing dashboard, alert, and chat readers remain authoritative.';
COMMENT ON TABLE asset_state_history IS
    'Append-only G3 semantic state history; G5 retention requires an explicit approved policy.';
COMMENT ON COLUMN asset_state_current.evidence_summary IS
    'Versioned evidence summary; source/series diagnostics are evidence only and never imply OFFLINE/UNAVAILABLE.';

INSERT INTO schema_migrations(version) VALUES ('012_asset_state_shadow')
ON CONFLICT (version) DO NOTHING;

COMMIT;
