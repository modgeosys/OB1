#!/usr/bin/env python3
"""Re-embed thoughts whose embedding is NULL (edited or never embedded).

Calls the `reembed_pending` MCP tool in a loop until no rows remain, so each
edge function invocation stays short and the user can Ctrl-C between batches
without losing committed work.

Usage:
    python reembed-thoughts.py --org ic
    python reembed-thoughts.py --org personal --batch-size 100
    python reembed-thoughts.py --org ic --dry-run
    python reembed-thoughts.py --org ic --once
"""

import argparse
import json
import os
import sys
import time

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
DEFAULT_BATCH_SIZE = 50


def call_reembed(org_url: str, access_key: str, batch_size: int, dry_run: bool, max_retries: int = 3) -> dict | None:
    """Call the reembed_pending MCP tool. Returns parsed result dict or None on failure."""
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "reembed_pending",
            "arguments": {"batch_size": batch_size, "dry_run": dry_run},
        },
        "id": 1,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    for attempt in range(max_retries):
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
                    print(f"  ⚠ Tool error: {error_text}", file=sys.stderr)
                    return None
                text = result["content"][0]["text"]
                return json.loads(text)
            return None
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                delay = 2 ** attempt
                print(f"  ⚠ Network error (retry {attempt + 1}/{max_retries} in {delay}s): {e}", file=sys.stderr)
                time.sleep(delay)
            else:
                print(f"  ⚠ Network error (giving up after {max_retries} attempts): {e}", file=sys.stderr)
                return None
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"  ⚠ Unexpected response shape: {e}", file=sys.stderr)
            return None


def main():
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description="Re-embed thoughts with NULL embeddings")
    parser.add_argument("--org", choices=list(ORGS.keys()), required=True, help="Target organization")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help=f"Rows per batch (default: {DEFAULT_BATCH_SIZE}, max 500)")
    parser.add_argument("--dry-run", action="store_true", help="Report counts without writing")
    parser.add_argument("--once", action="store_true", help="Run a single batch instead of looping")
    parser.add_argument("--key", help="Open Brain MCP access key (default: $OPEN_BRAIN_KEY_<ORG>)")
    args = parser.parse_args()

    org_config = ORGS[args.org]
    org_url = org_config["url"]
    access_key = args.key or os.environ.get(org_config["key_env"])

    if not access_key:
        print(f"Error: No access key provided. Use --key or set {org_config['key_env']} env var.", file=sys.stderr)
        sys.exit(1)

    print(f"Target: {args.org} ({org_url})")
    print(f"Batch size: {args.batch_size}  dry_run: {args.dry_run}  once: {args.once}")

    total_processed = 0
    all_failed: list[dict] = []
    batch_num = 0

    while True:
        batch_num += 1
        result = call_reembed(org_url, access_key, args.batch_size, args.dry_run)

        if result is None:
            print(f"[{batch_num}] Failed — aborting loop", file=sys.stderr)
            sys.exit(1)

        processed = result.get("processed", 0)
        remaining = result.get("remaining", 0)
        failed = result.get("failed", [])

        total_processed += processed
        all_failed.extend(failed)

        print(f"[{batch_num}] Processed {processed}, {remaining} remaining" + (f", {len(failed)} failed" if failed else ""))

        if args.dry_run:
            break
        if args.once:
            break
        if remaining == 0:
            break
        if processed == 0 and not args.dry_run:
            print("  ⚠ Batch processed 0 rows but remaining > 0 — stopping to avoid infinite loop", file=sys.stderr)
            break

    print(f"\n{'=' * 50}")
    print(f"Done.")
    print(f"  Total processed: {total_processed}")
    print(f"  Total failed:    {len(all_failed)}")
    if all_failed:
        print(f"  Failed IDs:")
        for f in all_failed:
            print(f"    {f.get('id')}: {f.get('error')}")


if __name__ == "__main__":
    main()
