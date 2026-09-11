#!/usr/bin/env python3
"""
Bulk-add MD5s to a running Stacks instance's download queue.

Usage:
    python3 search.py "some query" | awk '{print $1}' | python3 queue_to_stacks.py
    python3 queue_to_stacks.py <md5-1> <md5-2> ...

Reads MD5s either from positional args or one per line on stdin (blank lines
and lines starting with # are ignored). Requires the Stacks Admin API key,
either via --api-key or the STACKS_API_KEY environment variable.
"""
import argparse
import os
import sys
import urllib.request
import json


def add_one(base_url, api_key, md5, subfolder=None):
    payload = {"md5": md5, "source": "zlib-index-bulk"}
    if subfolder:
        payload["subfolder"] = subfolder
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/queue/add",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            return True, body.get("message", "queued")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            return False, body.get("error", str(e))
        except Exception:
            return False, str(e)
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("md5s", nargs="*", help="MD5s to queue (or read from stdin)")
    parser.add_argument("--base-url", default="http://localhost:7788")
    parser.add_argument("--api-key", default=os.environ.get("STACKS_API_KEY"))
    parser.add_argument("--subfolder", default=None)
    args = parser.parse_args()

    if not args.api_key:
        print("error: no API key given (--api-key or STACKS_API_KEY env var)", file=sys.stderr)
        sys.exit(1)

    md5s = list(args.md5s)
    if not md5s:
        for line in sys.stdin:
            line = line.strip()
            if line and not line.startswith("#"):
                md5s.append(line)

    if not md5s:
        print("no MD5s given", file=sys.stderr)
        sys.exit(1)

    ok, fail = 0, 0
    for md5 in md5s:
        success, message = add_one(args.base_url, args.api_key, md5, args.subfolder)
        status = "OK  " if success else "FAIL"
        print(f"{status} {md5}  {message}")
        if success:
            ok += 1
        else:
            fail += 1

    print(f"\nQueued {ok} book(s), {fail} failure(s).", file=sys.stderr)


if __name__ == "__main__":
    main()
