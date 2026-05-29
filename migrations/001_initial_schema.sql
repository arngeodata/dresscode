-- Dresscode initial schema
-- Already applied to Supabase project byhypleahdvigczhgozx (dresscode)
-- Kept here for reference and re-deployment to new environments

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE organisations (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                TEXT NOT NULL,
    email_username      TEXT NOT NULL UNIQUE,
    allowed_domains     TEXT[] NOT NULL DEFAULT '{}',
    tier                TEXT NOT NULL DEFAULT 'starter' CHECK (tier IN ('starter', 'growth', 'studio')),
    cv_limit            INTEGER,
    cv_count            INTEGER NOT NULL DEFAULT 0,
    billing_period_end  DATE NOT NULL DEFAULT (CURRENT_DATE + INTERVAL '1 month'),
    stripe_customer_id  TEXT,
    stripe_sub_id       TEXT,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE templates (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id          UUID NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    storage_path    TEXT NOT NULL,
    is_default      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX templates_one_default_per_org
    ON templates (org_id) WHERE is_default = TRUE;

CREATE TABLE async_jobs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id              UUID NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    sender_email        TEXT NOT NULL,
    original_filename   TEXT,
    input_path          TEXT NOT NULL,
    output_path         TEXT,
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'complete', 'failed')),
    error_message       TEXT,
    claude_tokens_in    INTEGER,
    claude_tokens_out   INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX async_jobs_pending_idx ON async_jobs (created_at ASC) WHERE status = 'pending';

CREATE OR REPLACE FUNCTION set_cv_limit_from_tier()
RETURNS TRIGGER AS $$
BEGIN
    NEW.cv_limit := CASE NEW.tier
        WHEN 'starter' THEN 50
        WHEN 'growth'  THEN 200
        WHEN 'studio'  THEN NULL
        ELSE NEW.cv_limit
    END;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_set_cv_limit
    BEFORE INSERT OR UPDATE OF tier ON organisations
    FOR EACH ROW EXECUTE FUNCTION set_cv_limit_from_tier();

CREATE OR REPLACE FUNCTION reset_monthly_cv_counts()
RETURNS void AS $$
BEGIN
    UPDATE organisations
    SET
        cv_count           = 0,
        billing_period_end = billing_period_end + INTERVAL '1 month'
    WHERE billing_period_end <= CURRENT_DATE AND active = TRUE;
END;
$$ LANGUAGE plpgsql;
