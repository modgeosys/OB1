-- The trigger function compares vector embeddings via IS DISTINCT FROM, which
-- needs the `vector = vector` operator from the `extensions` schema. Without
-- a SET search_path on the function, that operator isn't resolvable when the
-- trigger fires, and any UPDATE that touches `embedding` errors out with:
--   operator does not exist: extensions.vector = extensions.vector
--
-- Fix: re-create the function with `extensions` on its search_path. Triggers
-- referencing this function continue to work without recreation.
--
-- Safe to re-run.

CREATE OR REPLACE FUNCTION invalidate_embedding_on_content_change()
RETURNS TRIGGER
SET search_path = extensions, public, pg_catalog
AS $$
BEGIN
    IF NEW.content IS DISTINCT FROM OLD.content
       AND NEW.embedding IS NOT DISTINCT FROM OLD.embedding THEN
        NEW.embedding := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
