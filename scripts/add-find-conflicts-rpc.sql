-- Adds a corpus-wide conflict-detection RPC to both schemas.
--
-- find_conflicts(similarity_threshold, include_flagged) returns every pair of
-- thoughts whose cosine similarity is at or above the threshold, ordered by
-- similarity descending. The pair is canonicalized as (a.id < b.id) so each
-- pair appears exactly once.
--
-- Used by scripts/find-conflicts.py and the find_all_conflicts MCP tool to
-- enumerate near-duplicates and contradictions for interactive review.
--
-- Cost: O(n^2) over rows with non-NULL embeddings. At personal scale this is
-- fast enough (Postgres + pgvector cosine ops). For very large corpora the
-- threshold filter eliminates most pairs early in the scan.
--
-- Safe to re-run.

CREATE OR REPLACE FUNCTION personal.find_conflicts(
    similarity_threshold FLOAT DEFAULT 0.75,
    include_flagged BOOLEAN DEFAULT FALSE
)
RETURNS TABLE (
    id1 UUID,
    id2 UUID,
    content1 TEXT,
    content2 TEXT,
    similarity FLOAT,
    metadata1 JSONB,
    metadata2 JSONB,
    created_at1 TIMESTAMPTZ,
    created_at2 TIMESTAMPTZ,
    flagged1 BOOLEAN,
    flagged2 BOOLEAN
)
LANGUAGE plpgsql
SET search_path = personal, extensions
AS $$
BEGIN
    RETURN QUERY
    SELECT a.id, b.id,
           a.content, b.content,
           (1 - (a.embedding <=> b.embedding))::FLOAT AS similarity,
           a.metadata, b.metadata,
           a.created_at, b.created_at,
           a.to_be_deleted, b.to_be_deleted
    FROM personal.thoughts a
    JOIN personal.thoughts b ON a.id < b.id
    WHERE a.embedding IS NOT NULL
      AND b.embedding IS NOT NULL
      AND (1 - (a.embedding <=> b.embedding)) >= similarity_threshold
      AND (include_flagged OR (NOT a.to_be_deleted AND NOT b.to_be_deleted))
    ORDER BY (1 - (a.embedding <=> b.embedding)) DESC;
END;
$$;

CREATE OR REPLACE FUNCTION ic.find_conflicts(
    similarity_threshold FLOAT DEFAULT 0.75,
    include_flagged BOOLEAN DEFAULT FALSE
)
RETURNS TABLE (
    id1 UUID,
    id2 UUID,
    content1 TEXT,
    content2 TEXT,
    similarity FLOAT,
    metadata1 JSONB,
    metadata2 JSONB,
    created_at1 TIMESTAMPTZ,
    created_at2 TIMESTAMPTZ,
    flagged1 BOOLEAN,
    flagged2 BOOLEAN
)
LANGUAGE plpgsql
SET search_path = ic, extensions
AS $$
BEGIN
    RETURN QUERY
    SELECT a.id, b.id,
           a.content, b.content,
           (1 - (a.embedding <=> b.embedding))::FLOAT AS similarity,
           a.metadata, b.metadata,
           a.created_at, b.created_at,
           a.to_be_deleted, b.to_be_deleted
    FROM ic.thoughts a
    JOIN ic.thoughts b ON a.id < b.id
    WHERE a.embedding IS NOT NULL
      AND b.embedding IS NOT NULL
      AND (1 - (a.embedding <=> b.embedding)) >= similarity_threshold
      AND (include_flagged OR (NOT a.to_be_deleted AND NOT b.to_be_deleted))
    ORDER BY (1 - (a.embedding <=> b.embedding)) DESC;
END;
$$;
