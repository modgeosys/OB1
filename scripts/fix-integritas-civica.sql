-- Replace bare "Integritas" with "Integritas Civica" in ic.thoughts.content,
-- handling possessive forms ("Integritas'" and "Integritas's" → "Integritas Civica's")
-- while preserving existing correct instances.
--
-- Strategy: protect "Integritas Civica" with a placeholder, fix all remaining
-- bare occurrences, then restore the placeholder.

UPDATE ic.thoughts
SET content =
  replace(
    replace(
      replace(
        replace(
          replace(content,
            'Integritas Civica', '%%IC_PROTECTED%%'),
          E'Integritas''s', E'%%IC_PROTECTED%%''s'),
        E'Integritas''', E'%%IC_PROTECTED%%''s'),
      'Integritas', '%%IC_PROTECTED%%'),
    '%%IC_PROTECTED%%', 'Integritas Civica')
WHERE content LIKE '%Integritas%';
