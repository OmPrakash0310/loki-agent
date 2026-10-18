"""Start an investigation by hand: "something's wrong, Loki, look".

Set LOKI_API_KEY to a key with the write scope when Loki has LOKI_API_KEYS set.

Usage:
    python3 scripts/investigate.py --service payments-svc --description "checkout failing"
    python3 scripts/investigate.py --service shop-api --description "slow pages" --severity critical --agent http://loki:8080
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

AGENT = "http://localhost:8088"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--service", required=True)
    ap.add_argument("--description", required=True, help="what is wrong, in plain words")
    ap.add_argument("--severity", choices=["critical", "warning", "info"])
    ap.add_argument("--agent", default=AGENT, help=f"Loki's URL (default {AGENT})")
    return ap.parse_args(argv)


def build_request(args) -> urllib.request.Request:
    body = {"service": args.service, "description": args.description}
    if args.severity:
        body["severity"] = args.severity
    headers = {"Content-Type": "application/json"}
    if os.environ.get("LOKI_API_KEY"):  # needs a key with the write scope
        headers["Authorization"] = f"Bearer {os.environ['LOKI_API_KEY']}"
    return urllib.request.Request(f"{args.agent.rstrip('/')}/investigations/manual", data=json.dumps(body).encode(),
                                  headers=headers, method="POST")


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        with urllib.request.urlopen(build_request(args), timeout=30) as resp:
            result = json.load(resp)
    except urllib.error.HTTPError as e:
        print(f"Loki said {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"could not reach Loki at {args.agent}: {e.reason}", file=sys.stderr)
        return 1
    inv = result.get("investigation_id")
    how = "started" if result.get("result") == "new" else "joined the running"
    print(f"Incident #{result.get('incident_id')} {how} investigation #{inv}.")
    print(f"Report (in about a minute): {args.agent.rstrip('/')}/investigations/{inv}/report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
