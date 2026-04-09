#!/usr/bin/env python3
"""Import persistent knowledge from an Obsidian vault into Open Brain.

Usage:
    python import-obsidian.py /path/to/vault
    python import-obsidian.py /path/to/vault --dry-run
    python import-obsidian.py /path/to/vault --auto
    python import-obsidian.py /path/to/vault --exclude "daily-notes" "archive"
    python import-obsidian.py /path/to/vault1 /path/to/vault2
"""

import argparse
import json
import os
import re
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
MIN_CHARS = 200
MAX_TRANSCRIPT_CHARS = 12000  # Keep within model context window
DEFAULT_EXCLUDES = {".obsidian", ".trash", "templates", ".git"}
FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n?", re.DOTALL)

EXTRACTION_PROMPT = """Analyze this personal note from the user's knowledge base. Extract ONLY persistent knowledge — facts, preferences, decisions, relationships, and context that would be useful for a different AI to know about this user in the future.

Ignore: boilerplate, templates, empty scaffolding, task lists with no context, generic reference material copied from elsewhere.
Keep: personal details, professional context, preferences, recurring themes, important decisions, people mentioned with context, project details, organizational affiliations.

Return a JSON array of standalone statements. Each statement should make sense on its own to someone with no context. Return an empty array [] if nothing persistent is found.

Example output:
[
  "User works as a data scientist at Acme Corp",
  "User prefers Python over JavaScript for backend work",
  "User is building a personal knowledge management system using Obsidian"
]"""


def discover_notes(vault_root: Path, excludes: set[str], min_chars: int) -> list[dict]:
    """Walk an Obsidian vault and return notes that meet the minimum length."""
    notes = []

    for dirpath, dirs, files in os.walk(vault_root):
        # Prune excluded directories in-place
        dirs[:] = [d for d in dirs if d not in excludes]

        for filename in files:
            if not filename.endswith(".md"):
                continue

            filepath = Path(dirpath) / filename
            try:
                raw = filepath.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                print(f"  Warning: skipping non-UTF-8 file {filepath}", file=sys.stderr)
                continue

            # Strip YAML frontmatter
            body = FRONTMATTER_RE.sub("", raw, count=1).strip()

            if len(body) < min_chars:
                continue

            notes.append({
                "title": filepath.stem,
                "body": body,
                "path": str(filepath.relative_to(vault_root)),
            })

    # Sort by path for deterministic ordering
    notes.sort(key=lambda n: n["path"])
    return notes


def build_transcript(note: dict) -> str | None:
    """Format a note for knowledge extraction."""
    transcript = f"Note: {note['title']}\n\n{note['body']}"

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
    parser = argparse.ArgumentParser(description="Import Obsidian vault notes into Open Brain")
    parser.add_argument("dirs", nargs="+", help="Path(s) to Obsidian vault directories")
    parser.add_argument("--org", choices=list(ORGS.keys()), default="personal", help="Target organization (default: personal)")
    parser.add_argument("--dry-run", action="store_true", help="Preview extractions without saving")
    parser.add_argument("--auto", action="store_true", help="Save all items without interactive review")
    parser.add_argument("--key", help="Open Brain MCP access key (default: $OPEN_BRAIN_KEY_<ORG>)")
    parser.add_argument("--ollama-model", default=OLLAMA_MODEL, help=f"Ollama model for extraction (default: {OLLAMA_MODEL})")
    parser.add_argument("--min-chars", type=int, default=MIN_CHARS, help=f"Skip notes shorter than N chars (default: {MIN_CHARS})")
    parser.add_argument("--exclude", nargs="*", default=[], help="Additional directory names to skip")
    args = parser.parse_args()

    org_config = ORGS[args.org]
    org_url = org_config["url"]
    access_key = args.key or os.environ.get(org_config["key_env"])

    if not access_key:
        print(f"Error: No access key provided. Use --key or set {org_config['key_env']} env var.", file=sys.stderr)
        sys.exit(1)

    excludes = DEFAULT_EXCLUDES | set(args.exclude)

    # Discover notes from all vault directories
    all_notes: list[dict] = []
    for dir_path in args.dirs:
        root = Path(dir_path)
        if not root.is_dir():
            print(f"Error: {root} is not a directory", file=sys.stderr)
            sys.exit(1)
        notes = discover_notes(root, excludes, args.min_chars)
        print(f"Found {len(notes)} notes in {root}")
        all_notes.extend(notes)

    if not all_notes:
        print("No notes found matching criteria.")
        sys.exit(0)

    print(f"Total: {len(all_notes)} notes (target: {args.org})")

    total_extracted = 0
    total_saved = 0
    total_skipped_empty = 0
    quit_requested = False

    for i, note in enumerate(all_notes):
        if quit_requested:
            break

        transcript = build_transcript(note)

        if transcript is None:
            continue

        print(f"\n[{i + 1}/{len(all_notes)}] {note['path']}")
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
    print(f"  Notes processed:         {len(all_notes)}")
    print(f"  Skipped (no knowledge):  {total_skipped_empty}")
    print(f"  Items extracted:         {total_extracted}")
    print(f"  Items saved:             {total_saved}")


if __name__ == "__main__":
    main()
