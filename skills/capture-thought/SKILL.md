---
name: capture-thought
description: |
  Capture a thought to Open Brain, automatically routing to the correct
  organization instance based on the project's CLAUDE.md configuration.
  Trigger on: "remember this", "save this thought", "note that...",
  "capture this", or any request to persist an idea/observation/decision.
author: Kevin Weller
version: 1.0.0
---

# Capture Thought

## Overview

Route thought capture to the correct Open Brain instance based on the current project's configuration. Ensures thoughts land in the right organization brain without the user having to specify which one every time.

## When to Use

- User says "remember this", "save this thought", "note that..."
- User says "capture this to brain" or similar
- User asks to persist a decision, observation, idea, or reference
- User wants to record a person-note or contact insight

## Trigger Conditions

- Phrases: "remember this", "save this thought", "note that", "capture this", "add to brain"
- Intent: any request to persist information for future retrieval
- Context: works across any project with Open Brain MCP configured

## Process

1. **Determine target instance:**
   - Read the current project's CLAUDE.md for an Open Brain routing directive
   - Look for patterns like "Use `open-brain-ic`" or "Use `open-brain-personal`"
   - If no project-level directive found, default to `open-brain-personal`

2. **Prepare the thought:**
   - If the thought isn't already a clear, standalone statement, rewrite it to be self-contained
   - A good thought makes sense when retrieved later by any AI without surrounding context
   - Include relevant proper nouns, dates, and specifics rather than pronouns or relative references

3. **Capture via MCP:**
   - Call `capture_thought` on the determined instance
   - Pass the prepared content string

4. **Report results:**
   - Confirm which instance received the thought
   - Report extracted metadata: type, topics, people, action items (if any)

## Output

Confirmation message showing:
- Which brain instance received the thought (e.g., "Saved to open-brain-ic")
- Extracted type (observation, task, idea, reference, person_note)
- Extracted topics (1-3 tags)
- Any people or action items detected

## Examples

**User in IC project says:** "Remember that Russell Mark is a potential board member with nonprofit governance experience"
- Instance: `open-brain-ic` (project CLAUDE.md specifies this)
- Captured as: "Russell Mark is a potential board member for Integritas Civica. He has nonprofit governance experience."
- Type: person_note
- Topics: ["board-recruitment", "governance"]
- People: ["Russell Mark"]

**User in personal project says:** "Note that the dentist appointment is May 15"
- Instance: `open-brain-personal` (default, no project directive)
- Captured as: "Dentist appointment scheduled for 2026-05-15."
- Type: task
- Topics: ["health", "appointments"]
- Dates: ["2026-05-15"]

## Notes

- The routing logic reads CLAUDE.md at the project root -- it does not hardcode paths
- If multiple Open Brain instances are configured but no directive exists, always default to `open-brain-personal`
- The skill does NOT decide routing based on content analysis -- it uses the project context exclusively
- If the MCP server is unreachable, report the error clearly and suggest the user check their MCP configuration
