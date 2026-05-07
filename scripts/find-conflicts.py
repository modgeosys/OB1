#!/usr/bin/env python3
"""Scan an Open Brain corpus for pairs of thoughts whose semantic similarity
is at or above a threshold. Writes a JSON report and prints a summary.

The output JSON is consumed by the /curate-thoughts skill (or any reviewer)
to walk through pairs interactively and decide replace / merge / flag / skip.

Usage:
    python find-conflicts.py --org ic
    python find-conflicts.py --org personal --threshold 0.85
    python find-conflicts.py --org ic --include-flagged
    python find-conflicts.py --org ic --output ic-conflicts.json
"""

import argparse
import json
import os
import sys
from datetime import datetime

import requests

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
DEFAULT_THRESHOLD = 0.75
DEFAULT_LIMIT = 1000


def call_find_all_conflicts(
    org_url: str,
    access_key: str,
    threshold: float,
    include_flagged: bool,
    limit: int,
) -> dict | None:
    """Call the find_all_conflicts MCP tool. Returns parsed dict or None on failure."""
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "find_all_conflicts",
            "arguments": {
                "threshold": threshold,
                "include_flagged": include_flagged,
                "limit": limit,
            },
        },
        "id": 1,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    try:
        r = requests.post(
            f"{org_url}?key={access_key}",
            json=payload,
            headers=headers,
            timeout=600,
        )
        r.raise_for_status()

        for line in r.text.splitlines():
            if not line.startswith("data: "):
                continue
            data = json.loads(line[6:])
            result = data.get("result")
            if not result:
                continue
            if result.get("isError"):
                error_text = result["content"][0]["text"]
                print(f"  Tool error: {error_text}", file=sys.stderr)
                return None
            text = result["content"][0]["text"]
            return json.loads(text)
        return None
    except requests.RequestException as e:
        print(f"  Network error: {e}", file=sys.stderr)
        return None
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  Unexpected response shape: {e}", file=sys.stderr)
        return None


def summarize(report: dict) -> None:
    """Print a human-readable summary to stdout."""
    pairs = report.get("pairs", [])
    print(f"Total pairs found: {report['total_pairs']}")
    print(f"Pairs in report:   {report['returned']}")
    print(f"Threshold:         {report['threshold']}")
    print(f"Include flagged:   {report['include_flagged']}")
    print()

    if not pairs:
        print("No conflicts found at this threshold.")
        return

    buckets = {"0.95+": 0, "0.90-0.95": 0, "0.85-0.90": 0, "0.80-0.85": 0, "<0.80": 0}
    for p in pairs:
        s = p["similarity"]
        if s >= 0.95:
            buckets["0.95+"] += 1
        elif s >= 0.90:
            buckets["0.90-0.95"] += 1
        elif s >= 0.85:
            buckets["0.85-0.90"] += 1
        elif s >= 0.80:
            buckets["0.80-0.85"] += 1
        else:
            buckets["<0.80"] += 1

    print("Similarity distribution:")
    for label, count in buckets.items():
        if count > 0:
            print(f"  {label}: {count}")
    print()

    print("Top 5 pairs:")
    for i, p in enumerate(pairs[:5], 1):
        print(f"\n[{i}] similarity={p['similarity']:.4f}")
        print(f"    A ({p['id1']}): {p['content1'][:120]}{'...' if len(p['content1']) > 120 else ''}")
        print(f"    B ({p['id2']}): {p['content2'][:120]}{'...' if len(p['content2']) > 120 else ''}")


def main():
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description="Find conflicting/near-duplicate thoughts")
    parser.add_argument("--org", choices=list(ORGS.keys()), required=True, help="Target organization")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Cosine similarity threshold 0-1 (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--include-flagged",
        action="store_true",
        help="Include pairs where either thought is flagged for removal",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max pairs to fetch (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument("--output", help="Output JSON path (default: conflicts-<org>-<timestamp>.json)")
    parser.add_argument("--key", help="Open Brain MCP access key (default: $OPEN_BRAIN_KEY_<ORG>)")
    args = parser.parse_args()

    if not 0.0 <= args.threshold <= 1.0:
        print("Error: --threshold must be between 0 and 1", file=sys.stderr)
        sys.exit(1)

    org_config = ORGS[args.org]
    org_url = org_config["url"]
    access_key = args.key or os.environ.get(org_config["key_env"])

    if not access_key:
        print(f"Error: No access key provided. Use --key or set {org_config['key_env']} env var.", file=sys.stderr)
        sys.exit(1)

    output_path = args.output or f"conflicts-{args.org}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"

    print(f"Target:     {args.org} ({org_url})")
    print(f"Threshold:  {args.threshold}")
    print(f"Limit:      {args.limit}")
    print(f"Flagged:    {'included' if args.include_flagged else 'excluded'}")
    print(f"Output:     {output_path}")
    print()
    print("Scanning corpus...")

    report = call_find_all_conflicts(
        org_url, access_key, args.threshold, args.include_flagged, args.limit
    )
    if report is None:
        print("Failed to retrieve conflicts.", file=sys.stderr)
        sys.exit(1)

    report["org"] = args.org
    report["generated_at"] = datetime.now().isoformat()

    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)

    print()
    summarize(report)
    print()
    print(f"Full report written to {output_path}")


if __name__ == "__main__":
    main()
