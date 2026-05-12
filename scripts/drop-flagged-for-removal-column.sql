-- Drops the legacy `flagged_for_removal` column from personal.thoughts and
-- ic.thoughts. The active column is now `to_be_deleted`; this script:
--
-- 1. OR-merges any TRUE values from `flagged_for_removal` into
--    `to_be_deleted` before dropping, so no flagged rows are forgotten.
-- 2. Drops the legacy index if one was created in an earlier migration.
-- 3. Drops the column.
--
-- Each step is guarded by an existence check so the script is safe to re-run
-- and safe on either schema even if one is already cleaned up.

DO $$
DECLARE
    s TEXT;
BEGIN
    FOREACH s IN ARRAY ARRAY['personal', 'ic']
    LOOP
        -- Step 1: merge legacy flag into to_be_deleted (only if old column present).
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = s
              AND table_name = 'thoughts'
              AND column_name = 'flagged_for_removal'
        ) THEN
            EXECUTE format(
                'UPDATE %I.thoughts
                 SET to_be_deleted = TRUE
                 WHERE flagged_for_removal = TRUE AND to_be_deleted = FALSE',
                s
            );

            -- Step 2: drop any index referencing the legacy column.
            EXECUTE format(
                'DROP INDEX IF EXISTS %I.idx_%I_thoughts_flagged_for_removal',
                s, s
            );

            -- Step 3: drop the column.
            EXECUTE format(
                'ALTER TABLE %I.thoughts DROP COLUMN IF EXISTS flagged_for_removal',
                s
            );

            RAISE NOTICE 'Dropped flagged_for_removal from %.thoughts', s;
        ELSE
            RAISE NOTICE 'No flagged_for_removal column on %.thoughts (already clean)', s;
        END IF;
    END LOOP;
END$$;
