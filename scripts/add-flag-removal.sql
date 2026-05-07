-- Adds flag-for-removal support to both schemas.
--
-- 1. Adds `to_be_deleted BOOLEAN NOT NULL DEFAULT FALSE` to thoughts.
-- 2. Refines the embedding-invalidation trigger so an UPDATE that supplies a
--    fresh embedding alongside new content is honored (the trigger only nulls
--    the embedding when content changed AND the caller did not also set it).
-- 3. Recreates match_thoughts with an `include_flagged` parameter (default
--    FALSE) so semantic search excludes flagged rows by default.
--
-- Safe to re-run.

-- 1. Column ----------------------------------------------------------------

ALTER TABLE personal.thoughts
    ADD COLUMN IF NOT EXISTS to_be_deleted BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE ic.thoughts
    ADD COLUMN IF NOT EXISTS to_be_deleted BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_personal_thoughts_to_be_deleted
    ON personal.thoughts (to_be_deleted)
    WHERE to_be_deleted = TRUE;

CREATE INDEX IF NOT EXISTS idx_ic_thoughts_to_be_deleted
    ON ic.thoughts (to_be_deleted)
    WHERE to_be_deleted = TRUE;

-- 2. Refined trigger function ---------------------------------------------

CREATE OR REPLACE FUNCTION invalidate_embedding_on_content_change()
RETURNS TRIGGER
-- extensions on search_path so the `vector = vector` operator (used by the
-- IS DISTINCT FROM check on NEW.embedding) is resolvable when the trigger
-- fires from a session whose search_path doesn't include extensions.
SET search_path = extensions, public, pg_catalog
AS $$
BEGIN
    -- Only null the embedding if content changed AND the caller did not also
    -- supply a fresh embedding in the same UPDATE. Lets update_thought write
    -- content + new embedding atomically without the trigger discarding it.
    IF NEW.content IS DISTINCT FROM OLD.content
       AND NEW.embedding IS NOT DISTINCT FROM OLD.embedding THEN
        NEW.embedding := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 3. match_thoughts: add include_flagged ----------------------------------
-- Drop the prior 4-parameter signature first, since adding a parameter
-- creates a new overload rather than replacing the existing function.

DROP FUNCTION IF EXISTS personal.match_thoughts(vector(1024), float, int, jsonb);
DROP FUNCTION IF EXISTS ic.match_thoughts(vector(1024), float, int, jsonb);

CREATE OR REPLACE FUNCTION personal.match_thoughts(
    query_embedding vector(1024),
    match_threshold FLOAT DEFAULT 0.5,
    match_count INT DEFAULT 10,
    filter JSONB DEFAULT '{}'::jsonb,
    include_flagged BOOLEAN DEFAULT FALSE
)
RETURNS TABLE (
    id UUID, content TEXT, metadata JSONB,
    similarity FLOAT, created_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SET search_path = personal, extensions
AS $$
BEGIN
    RETURN QUERY
    SELECT t.id, t.content, t.metadata,
        (1 - (t.embedding <=> query_embedding))::FLOAT AS similarity,
        t.created_at
    FROM personal.thoughts t
    WHERE 1 - (t.embedding <=> query_embedding) >= match_threshold
      AND (filter = '{}'::jsonb OR t.metadata @> filter)
      AND (include_flagged OR NOT t.to_be_deleted)
    ORDER BY t.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;

CREATE OR REPLACE FUNCTION ic.match_thoughts(
    query_embedding vector(1024),
    match_threshold FLOAT DEFAULT 0.5,
    match_count INT DEFAULT 10,
    filter JSONB DEFAULT '{}'::jsonb,
    include_flagged BOOLEAN DEFAULT FALSE
)
RETURNS TABLE (
    id UUID, content TEXT, metadata JSONB,
    similarity FLOAT, created_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SET search_path = ic, extensions
AS $$
BEGIN
    RETURN QUERY
    SELECT t.id, t.content, t.metadata,
        (1 - (t.embedding <=> query_embedding))::FLOAT AS similarity,
        t.created_at
    FROM ic.thoughts t
    WHERE 1 - (t.embedding <=> query_embedding) >= match_threshold
      AND (filter = '{}'::jsonb OR t.metadata @> filter)
      AND (include_flagged OR NOT t.to_be_deleted)
    ORDER BY t.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;
