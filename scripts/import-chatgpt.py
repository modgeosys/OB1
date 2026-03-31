#!/usr/bin/env python3
"""Import persistent knowledge from a ChatGPT data export into Open Brain.

Usage:
    python import-chatgpt.py /path/to/conversations.json
    python import-chatgpt.py /path/to/conversations.json --dry-run
    python import-chatgpt.py /path/to/conversations.json --auto
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

OLLAMA_BASE = "http://localhost:11434"
OLLAMA_MODEL = "qwen3.5:35b"
ORGS = {
    "personal": {
        "url": "http://localhost:8000/functions/v1/open-brain-personal",
        "key_env": "OPEN_BRAIN_KEY_PERSONAL",
    },
    "ic": {
        "url": "http://localhost:8000/functions/v1/open-brain-ic",
        "key_env": "OPEN_BRAIN_KEY_IC",
    },
}
MIN_MESSAGES = 3
MAX_TRANSCRIPT_CHARS = 12000  # Keep within model context window

EXTRACTION_PROMPT = """Analyze this conversation between a user and AI assistant. Extract ONLY persistent knowledge — facts, preferences, decisions, relationships, and context that would be useful for a different AI to know about this user in the future.

Ignore: troubleshooting steps, code debugging, one-off questions, ephemeral tasks, general knowledge questions, how-to requests.
Keep: personal details, professional context, preferences, recurring themes, important decisions, people mentioned with context, project details, organizational affiliations.

Return a JSON array of standalone statements. Each statement should make sense on its own to someone with no context. Return an empty array [] if nothing persistent is found.

Example output:
[
  "User works as a data scientist at Acme Corp",
  "User prefers Python over JavaScript for backend work",
  "User is building a personal knowledge management system using Obsidian"
]"""


def parse_conversations(path: Path) -> list[dict]:
    """Parse conversations.json and return list of conversation dicts."""
    with open(path) as f:
        data = json.load(f)

    # Handle both array format and single-object format
    if isinstance(data, list):
        return data
    return [data]


def build_transcript(conversation: dict) -> str | None:
    """Extract a readable transcript from a conversation. Returns None if too short."""
    title = conversation.get("title", "Untitled")
    mapping = conversation.get("mapping", {})
    messages = []

    if mapping:
        # Tree-structured format: extract messages from mapping
        for node in mapping.values():
            msg = node.get("message")
            if not msg:
                continue
            author = msg.get("author", {}).get("role", "unknown")
            if author not in ("user", "assistant"):
                continue
            content = msg.get("content", {})
            parts = content.get("parts", [])
            text = " ".join(str(p) for p in parts if isinstance(p, str)).strip()
            if text:
                messages.append((author, text))
    else:
        # Flat messages array format
        for msg in conversation.get("messages", []):
            role = msg.get("role", msg.get("author", {}).get("role", "unknown"))
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content", "")
            if isinstance(content, dict):
                parts = content.get("parts", [])
                content = " ".join(str(p) for p in parts if isinstance(p, str))
            if content.strip():
                messages.append((role, content.strip()))

    if len(messages) < MIN_MESSAGES:
        return None

    lines = [f"Conversation: {title}", ""]
    for role, text in messages:
        prefix = "User" if role == "user" else "Assistant"
        lines.append(f"{prefix}: {text}")

    transcript = "\n".join(lines)

    # Truncate if too long
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:MAX_TRANSCRIPT_CHARS] + "\n\n[... truncated]"

    return transcript


def extract_knowledge(transcript: str) -> list[str]:
    """Send transcript to Ollama and extract persistent knowledge items."""
    try:
        r = requests.post(
            f"{OLLAMA_BASE}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "format": "json",
                "stream": False,
                "messages": [
                    {"role": "system", "content": EXTRACTION_PROMPT},
                    {"role": "user", "content": transcript},
                ],
            },
            timeout=300,
        )
        r.raise_for_status()
        content = r.json()["message"]["content"]
        parsed = json.loads(content)

        # Handle both array and object-with-array responses
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item]
        if isinstance(parsed, dict):
            # Some models wrap the array in a key
            for value in parsed.values():
                if isinstance(value, list):
                    return [str(item) for item in value if item]
        return []
    except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
        print(f"  ⚠ Extraction error: {e}", file=sys.stderr)
        return []


def capture_thought(content: str, access_key: str, org_url: str) -> bool:
    """Save a thought to Open Brain via MCP JSON-RPC."""
    try:
        r = requests.post(
            f"{org_url}?key={access_key}",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "capture_thought",
                    "arguments": {"content": content},
                },
                "id": 1,
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=120,
        )
        r.raise_for_status()
        # Parse SSE response
        for line in r.text.splitlines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
                if data.get("result", {}).get("isError"):
                    error_text = data["result"]["content"][0]["text"]
                    print(f"  ⚠ Capture error: {error_text}", file=sys.stderr)
                    return False
                return True
        return False
    except requests.RequestException as e:
        print(f"  ⚠ Network error: {e}", file=sys.stderr)
        return False


def review_item(item: str, index: int, total: int) -> str:
    """Prompt user to review an extracted item. Returns action: y/n/e/a/q."""
    print(f"\n  [{index + 1}/{total}] {item}")
    while True:
        choice = input("  (y)es / (n)o / (e)dit / (a)ccept all / (q)uit > ").strip().lower()
        if choice in ("y", "n", "e", "a", "q", ""):
            return choice if choice else "y"
        print("  Invalid choice. Use y/n/e/a/q.")


def main():
    parser = argparse.ArgumentParser(description="Import ChatGPT conversations into Open Brain")
    parser.add_argument("file", type=Path, help="Path to conversations.json")
    parser.add_argument("--org", choices=list(ORGS.keys()), default="personal", help="Target organization (default: personal)")
    parser.add_argument("--dry-run", action="store_true", help="Preview extractions without saving")
    parser.add_argument("--auto", action="store_true", help="Save all items without interactive review")
    parser.add_argument("--key", help="Open Brain MCP access key (default: $OPEN_BRAIN_KEY_<ORG>)")
    parser.add_argument("--ollama-model", default=OLLAMA_MODEL, help=f"Ollama model for extraction (default: {OLLAMA_MODEL})")
    parser.add_argument("--min-messages", type=int, default=MIN_MESSAGES, help=f"Skip conversations with fewer messages (default: {MIN_MESSAGES})")
    args = parser.parse_args()

    org_config = ORGS[args.org]
    org_url = org_config["url"]
    access_key = args.key or os.environ.get(org_config["key_env"])

    if not access_key:
        print(f"Error: No access key provided. Use --key or set {org_config['key_env']} env var.", file=sys.stderr)
        sys.exit(1)

    if not args.file.exists():
        print(f"Error: {args.file} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {args.file}...")
    conversations = parse_conversations(args.file)
    print(f"Found {len(conversations)} conversations (target: {args.org})")

    # Sort by create_time if available
    conversations.sort(key=lambda c: c.get("create_time", 0))

    total_extracted = 0
    total_saved = 0
    total_skipped_short = 0
    total_skipped_empty = 0
    quit_requested = False

    for i, conv in enumerate(conversations):
        if quit_requested:
            break

        title = conv.get("title", "Untitled")
        transcript = build_transcript(conv)

        if transcript is None:
            total_skipped_short += 1
            continue

        print(f"\n[{i + 1}/{len(conversations)}] {title}")
        print(f"  Extracting knowledge...")

        items = extract_knowledge(transcript)

        if not items:
            total_skipped_empty += 1
            print(f"  No persistent knowledge found")
            continue

        total_extracted += len(items)
        print(f"  Found {len(items)} item(s)")

        if args.dry_run:
            for item in items:
                print(f"    • {item}")
            continue

        accept_all = args.auto

        for j, item in enumerate(items):
            if quit_requested:
                break

            if accept_all:
                print(f"  Saving: {item[:80]}{'...' if len(item) > 80 else ''}")
            else:
                action = review_item(item, j, len(items))

                if action == "q":
                    quit_requested = True
                    break
                elif action == "n":
                    continue
                elif action == "a":
                    accept_all = True
                elif action == "e":
                    edited = input("  Enter edited text: ").strip()
                    if not edited:
                        print("  Empty input, skipping")
                        continue
                    item = edited

            if capture_thought(item, access_key, org_url):
                total_saved += 1
                print(f"  ✓ Saved")
            else:
                print(f"  ✗ Failed to save")

    print(f"\n{'=' * 50}")
    print(f"Import complete.")
    print(f"  Conversations processed: {len(conversations)}")
    print(f"  Skipped (too short):     {total_skipped_short}")
    print(f"  Skipped (no knowledge):  {total_skipped_empty}")
    print(f"  Items extracted:         {total_extracted}")
    print(f"  Items saved:             {total_saved}")


if __name__ == "__main__":
    main()
