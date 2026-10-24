"""Read the production request ledger and correlate failure modes.

Prints counts and safe structural fields only. Never prints a prompt, an
image payload, a token, or a credential.
"""

from __future__ import annotations

import collections
import json

LEDGERS = (
    "/var/lib/gateway/request-ledger.jsonl",
    "/var/lib/gateway/request-ledger.jsonl.1",
)

# Never echoed back to the operator.
SENSITIVE = {
    "messages", "request", "body", "payload", "input", "output",
    "prompt", "headers", "authorization", "token", "response",
}


def load() -> list[dict]:
    rows: list[dict] = []
    for path in LEDGERS:
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            continue
    return rows


def kind_of(row: dict):
    return row.get("request_kind") or row.get("kind") or row.get("route_kind")


def main() -> None:
    rows = load()
    print("TOTAL", len(rows))
    if not rows:
        return
    print("KEYS", json.dumps(sorted(rows[0].keys())))

    print("KINDS", json.dumps(collections.Counter(str(kind_of(r)) for r in rows)))
    print("STREAMED", json.dumps(collections.Counter(str(r.get("streamed")) for r in rows)))
    print("OUTCOME", json.dumps(collections.Counter(str(r.get("outcome_class")) for r in rows)))

    malformed = [
        r for r in rows
        if "malformed" in json.dumps(
            {k: v for k, v in r.items() if k not in SENSITIVE}
        ).lower()
    ]
    print("MALFORMED", len(malformed))
    print("MAL_KINDS", json.dumps(collections.Counter(str(kind_of(r)) for r in malformed)))
    print("MAL_STREAM", json.dumps(collections.Counter(str(r.get("streamed")) for r in malformed)))
    print("MAL_ERRTYPE", json.dumps(collections.Counter(str(r.get("error_type")) for r in malformed)))
    print("MAL_SERVE", json.dumps(collections.Counter(
        f'{r.get("serving_provider")}/{r.get("serving_model")}' for r in malformed)))

    # Do malformed rows carry an image flag anywhere in their metadata?
    print("MAL_IMAGEFLAG", json.dumps(collections.Counter(
        str(r.get("has_image") or r.get("image_input") or r.get("needs_image_input"))
        for r in malformed)))

    print("--- MALFORMED SAMPLES (safe fields) ---")
    safe = (
        "timestamp_utc", "request_kind", "streamed", "requested_alias",
        "requested_model", "serving_provider", "serving_model",
        "outcome_class", "error_type", "error_class", "error_message",
        "has_image", "needs_image_input",
    )
    for row in malformed[-6:]:
        sample = {k: row.get(k) for k in safe if k in row}
        if sample.get("error_message"):
            sample["error_message"] = str(sample["error_message"])[:140]
        print(json.dumps(sample)[:700])

    # Same for the 503 no_eligible_route failures, for contrast.
    no_route = [
        r for r in rows
        if str(r.get("error_type") or r.get("error_class") or "").find("no_eligible_route") >= 0
        or "no eligible route" in str(r.get("error_message") or "").lower()
    ]
    print("NO_ROUTE", len(no_route))
    if no_route:
        print("NO_ROUTE_KINDS", json.dumps(collections.Counter(str(kind_of(r)) for r in no_route)))
        print("NO_ROUTE_STREAM", json.dumps(collections.Counter(str(r.get("streamed")) for r in no_route)))
        for row in no_route[-3:]:
            sample = {k: row.get(k) for k in safe if k in row}
            if sample.get("error_message"):
                sample["error_message"] = str(sample["error_message"])[:140]
            print("NR", json.dumps(sample)[:700])


if __name__ == "__main__":
    main()
