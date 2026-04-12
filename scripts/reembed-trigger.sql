-- Invalidate the embedding whenever a thought's content changes, so re-embedding
-- jobs can find dirty rows via `WHERE embedding IS NULL`. Applies to both the
-- `personal` and `ic` schemas used by Open Brain. Safe to re-run.

CREATE OR REPLACE FUNCTION invalidate_embedding_on_content_change()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.content IS DISTINCT FROM OLD.content THEN
        NEW.embedding := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS thoughts_invalidate_embedding ON personal.thoughts;
CREATE TRIGGER thoughts_invalidate_embedding
    BEFORE UPDATE ON personal.thoughts
    FOR EACH ROW
    EXECUTE FUNCTION invalidate_embedding_on_content_change();

DROP TRIGGER IF EXISTS thoughts_invalidate_embedding ON ic.thoughts;
CREATE TRIGGER thoughts_invalidate_embedding
    BEFORE UPDATE ON ic.thoughts
    FOR EACH ROW
    EXECUTE FUNCTION invalidate_embedding_on_content_change();
