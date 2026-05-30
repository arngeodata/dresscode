-- Add style_guide column to organisations
-- Stores per-agency formatting preferences as JSONB.
-- If NULL, the formatter falls back to DEFAULT_STYLE_GUIDE in formatter.py.
-- Populated during client onboarding via style_extractor.py.

ALTER TABLE organisations ADD COLUMN IF NOT EXISTS style_guide JSONB;
