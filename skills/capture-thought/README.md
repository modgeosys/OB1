# Capture Thought

Capture thoughts to Open Brain with automatic routing to the correct organization instance based on your project's CLAUDE.md configuration.

## What It Does

When you ask to "remember", "save", or "capture" a thought, this skill:
1. Reads your project's CLAUDE.md to determine which Open Brain instance to use
2. Rewrites the thought as a clear, standalone statement if needed
3. Calls `capture_thought` on the correct MCP instance
4. Reports confirmation with extracted metadata

## Supported Clients

- **Claude Code** (primary) -- reads CLAUDE.md project files natively
- Any MCP-aware AI client that supports skill files and has Open Brain MCP configured

## Prerequisites

- Open Brain MCP server running and accessible
- At least one Open Brain instance configured (e.g., `open-brain-personal`)
- MCP connection configured in your AI client settings
- (Optional) Multiple instances for multi-organization routing (e.g., `open-brain-ic`)

## Installation

### Claude Code

Copy the skill file to your Claude Code skills directory:

```bash
cp skills/capture-thought/SKILL.md ~/.claude/skills/capture-thought.md
```

Or reference it in your project's `.claude/settings.local.json`.

### Other Clients

Place `SKILL.md` in your client's custom prompts or rules directory. Adapt the MCP tool call syntax to match your client's format.

## Trigger Conditions

The skill activates when the user:
- Says "remember this", "save this thought", "note that..."
- Says "capture this to brain" or "add to brain"
- Asks to persist a decision, observation, idea, or reference
- Wants to record information about a person or contact

## Expected Outcome

After triggering, you should see:
- Confirmation of which brain instance received the thought
- Extracted metadata: type, topics, people (if any), action items (if any)
- The thought content as stored (may be rephrased for clarity)

## Routing Logic

The skill determines which instance to use by reading the project's CLAUDE.md:

| Project Directive | Instance Used |
|-------------------|---------------|
| "Use `open-brain-ic`" | `open-brain-ic` |
| "Use `open-brain-personal`" | `open-brain-personal` |
| No directive found | `open-brain-personal` (default) |

Routing is based on **project context**, not content analysis.

## Troubleshooting

### Wrong instance selected
- Check your project's CLAUDE.md for the Open Brain routing directive
- Ensure you're working in the correct project directory
- The skill uses the nearest CLAUDE.md in the directory hierarchy

### MCP connection failure
- Verify your MCP server is running (`curl` the health endpoint)
- Check MCP configuration in your AI client settings
- Ensure the access key is correct in environment variables

### Embedding generation timeout
- The Open Brain server generates embeddings via Ollama on capture
- If Ollama is slow or unresponsive, the capture may timeout
- Check that your Ollama instance is running and the embedding model is loaded
- The thought will still be saved but may have a NULL embedding (use `reembed_pending` later)
