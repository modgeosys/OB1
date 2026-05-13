-- GIN index on thoughts.metadata using jsonb_path_ops.
--
-- Enables cheap containment queries against the new metadata.sources array,
-- e.g.
--   WHERE metadata @> '{"sources":[{"system":"obsidian","locator":"foo.md"}]}'
--
-- jsonb_path_ops is the right operator class here: smaller index, faster for
-- the @> containment predicate we actually use. The trade-off (no support for
-- ?, ?|, ?& key-exists operators) is irrelevant for the sources query shape.
--
-- Safe to re-run.

CREATE INDEX IF NOT EXISTS idx_personal_thoughts_metadata
    ON personal.thoughts USING gin (metadata jsonb_path_ops);

CREATE INDEX IF NOT EXISTS idx_ic_thoughts_metadata
    ON ic.thoughts USING gin (metadata jsonb_path_ops);
