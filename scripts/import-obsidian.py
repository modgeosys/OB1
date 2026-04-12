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
import time
from pathlib import Path

import requests

OLLAMA_BASE = "http://localhost:11434"
OLLAMA_MODEL = "qwen3.5:35b"
OLLAMA_TIMEOUT = 900  # seconds; large models with CPU offload can be slow
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

EXTRACTION_PROMPT_PERSONAL = """Analyze this personal note from the user's knowledge base. Extract ONLY persistent knowledge — facts, preferences, decisions, relationships, and context that would be useful for a different AI to know about this user in the future.

Ignore: boilerplate, templates, empty scaffolding, task lists with no context, generic reference material copied from elsewhere.
Keep: personal details, professional context, preferences, recurring themes, important decisions, people mentioned with context, project details, organizational affiliations.

Return a JSON array of standalone statements. Each statement should make sense on its own to someone with no context. Return an empty array [] if nothing persistent is found.

Example output:
[
  "User works as a data scientist at Acme Corp",
  "User prefers Python over JavaScript for backend work",
  "User is building a personal knowledge management system using Obsidian"
]"""

EXTRACTION_PROMPT_ORG = """Analyze this note from an organization's knowledge base. Extract ONLY persistent knowledge about the organization — facts, decisions, strategy, processes, contacts, and context that would be useful for a different AI working with this organization in the future.

Ignore: boilerplate, templates, empty scaffolding, task lists with no context, generic reference material copied from elsewhere.
Keep: the organization's vision, mission, philosophy, strategy, methods, and basic factual knowledge; decisions; key people and their roles; processes and workflows; project status and goals; partnerships; policies; institutional knowledge; and the user's organization-related preferences and working style.

Return a JSON array of standalone statements. Each statement should make sense on its own to someone with no context. Return an empty array [] if nothing persistent is found.

Example output:
[
  "The organization uses a two-week sprint cycle with Monday standups",
  "Sarah Chen is the lead designer responsible for the mobile app redesign",
  "The board approved a pivot to B2B sales in Q1 2026"
]"""


def load_single_note(filepath: Path, min_chars: int) -> dict | None:
    """Load a single .md file as a note dict. Returns None if unreadable or too short."""
    try:
        raw = filepath.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(f"  Warning: {filepath} is not UTF-8", file=sys.stderr)
        return None

    body = FRONTMATTER_RE.sub("", raw, count=1).strip()
    if len(body) < min_chars:
        print(f"  Warning: {filepath} body is {len(body)} chars, below --min-chars {min_chars}", file=sys.stderr)
        return None

    return {
        "title": filepath.stem,
        "body": body,
        "path": str(filepath),
    }


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


def build_chunks(note: dict, max_chars: int = MAX_TRANSCRIPT_CHARS) -> list[str]:
    """Split a note into transcript chunks that fit the model context window.

    Splits at paragraph boundaries (double newlines), then single newlines, then
    hard-splits as a last resort. Each chunk is prefixed with the note title so
    the model always has context about what it's reading.
    """
    title = note["title"]
    body = note["body"]

    # Reserve space for the header and a small safety margin
    header_template = f"Note: {title} (part {{n}}/{{total}})\n\n"
    single_header = f"Note: {title}\n\n"
    budget = max_chars - len(header_template.format(n=99, total=99)) - 100

    # Fast path: whole note fits in one chunk
    if len(body) <= max_chars - len(single_header) - 100:
        return [single_header + body]

    # Split into paragraphs and pack them into chunks
    paragraphs = body.split("\n\n")
    raw_chunks: list[str] = []
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            raw_chunks.append(current.strip())
        current = ""

    for para in paragraphs:
        # If a single paragraph exceeds budget, try splitting on single newlines
        if len(para) > budget:
            flush()
            lines = para.split("\n")
            sub = ""
            for line in lines:
                if len(sub) + len(line) + 1 > budget:
                    if sub.strip():
                        raw_chunks.append(sub.strip())
                    sub = ""
                    # Still too big? Hard-split.
                    if len(line) > budget:
                        for i in range(0, len(line), budget):
                            raw_chunks.append(line[i:i + budget])
                        continue
                sub += line + "\n"
            if sub.strip():
                raw_chunks.append(sub.strip())
            continue

        if len(current) + len(para) + 2 > budget:
            flush()
        current += para + "\n\n"

    flush()

    # Prepend headers with part numbers
    total = len(raw_chunks)
    return [f"Note: {title} (part {n + 1}/{total})\n\n{c}" for n, c in enumerate(raw_chunks)]


def extract_knowledge(transcript: str, prompt: str, model: str = OLLAMA_MODEL, timeout: int = OLLAMA_TIMEOUT, max_retries: int = 3) -> list[str]:
    """Send transcript to Ollama and extract persistent knowledge items.

    Retries on network errors (exponential backoff) and on malformed JSON from
    the model (immediate retry — re-sampling usually produces valid JSON).
    """
    for attempt in range(max_retries):
        try:
            r = requests.post(
                f"{OLLAMA_BASE}/api/chat",
                json={
                    "model": model,
                    "format": "json",
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": transcript},
                    ],
                },
                timeout=timeout,
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
        except json.JSONDecodeError as e:
            if attempt < max_retries - 1:
                print(f"  ⚠ Malformed JSON from model (retry {attempt + 1}/{max_retries}): {e}", file=sys.stderr)
            else:
                print(f"  ⚠ Extraction error (giving up after {max_retries} attempts): {e}", file=sys.stderr)
                return []
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                delay = 2 ** attempt
                print(f"  ⚠ Network error (retry {attempt + 1}/{max_retries} in {delay}s): {e}", file=sys.stderr)
                time.sleep(delay)
            else:
                print(f"  ⚠ Extraction error (giving up after {max_retries} attempts): {e}", file=sys.stderr)
                return []
        except KeyError as e:
            print(f"  ⚠ Unexpected response shape from model: {e}", file=sys.stderr)
            return []

    return []


def capture_thought(content: str, access_key: str, org_url: str, max_retries: int = 3) -> bool:
    """Save a thought to Open Brain via MCP JSON-RPC. Retries on transient errors."""
    for attempt in range(max_retries):
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
            if attempt < max_retries - 1:
                delay = 2 ** attempt
                print(f"  ⚠ Network error (retry {attempt + 1}/{max_retries} in {delay}s): {e}", file=sys.stderr)
                time.sleep(delay)
            else:
                print(f"  ⚠ Network error (giving up after {max_retries} attempts): {e}", file=sys.stderr)
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
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description="Import Obsidian vault notes into Open Brain")
    parser.add_argument("paths", nargs="+", help="Path(s) to Obsidian vault directories or individual .md files")
    parser.add_argument("--org", choices=list(ORGS.keys()), default="personal", help="Target organization (default: personal)")
    parser.add_argument("--dry-run", action="store_true", help="Preview extractions without saving")
    parser.add_argument("--auto", action="store_true", help="Save all items without interactive review")
    parser.add_argument("--key", help="Open Brain MCP access key (default: $OPEN_BRAIN_KEY_<ORG>)")
    parser.add_argument("--ollama-model", default=OLLAMA_MODEL, help=f"Ollama model for extraction (default: {OLLAMA_MODEL})")
    parser.add_argument("--ollama-timeout", type=int, default=OLLAMA_TIMEOUT, help=f"Ollama request timeout in seconds (default: {OLLAMA_TIMEOUT})")
    parser.add_argument("--min-chars", type=int, default=MIN_CHARS, help=f"Skip notes shorter than N chars (default: {MIN_CHARS})")
    parser.add_argument("--exclude", nargs="*", default=[], help="Additional directory names to skip")
    args = parser.parse_args()

    org_config = ORGS[args.org]
    org_url = org_config["url"]
    access_key = args.key or os.environ.get(org_config["key_env"])
    extraction_prompt = EXTRACTION_PROMPT_PERSONAL if args.org == "personal" else EXTRACTION_PROMPT_ORG

    if not access_key:
        print(f"Error: No access key provided. Use --key or set {org_config['key_env']} env var.", file=sys.stderr)
        sys.exit(1)

    excludes = DEFAULT_EXCLUDES | set(args.exclude)

    # Discover notes from vault directories and/or individual files
    all_notes: list[dict] = []
    for input_path in args.paths:
        p = Path(input_path)
        if p.is_dir():
            notes = discover_notes(p, excludes, args.min_chars)
            print(f"Found {len(notes)} notes in {p}")
            all_notes.extend(notes)
        elif p.is_file():
            if p.suffix.lower() != ".md":
                print(f"Error: {p} is not a .md file", file=sys.stderr)
                sys.exit(1)
            note = load_single_note(p, args.min_chars)
            if note is None:
                sys.exit(1)
            print(f"Loaded single file: {p}")
            all_notes.append(note)
        else:
            print(f"Error: {p} is not a directory or file", file=sys.stderr)
            sys.exit(1)

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

        chunks = build_chunks(note)

        print(f"\n[{i + 1}/{len(all_notes)}] {note['path']}")
        if len(chunks) > 1:
            print(f"  Note is large ({len(note['body'])} chars) — splitting into {len(chunks)} chunks")

        items: list[str] = []
        seen: set[str] = set()
        for chunk_idx, chunk in enumerate(chunks):
            if len(chunks) > 1:
                print(f"  Extracting knowledge (chunk {chunk_idx + 1}/{len(chunks)})...")
            else:
                print(f"  Extracting knowledge...")
            chunk_items = extract_knowledge(chunk, extraction_prompt, args.ollama_model, args.ollama_timeout)
            for item in chunk_items:
                key = item.strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    items.append(item)

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
                print(f"  Saving: {item}")
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
