"""Sanitize a captured PC Express response into a test fixture.

    python scripts/sanitize_fixture.py raw.json tests/fixtures/out.json [--max-tiles 12]

Removes customer identity (names, emails, PC ids), replaces cart/order/user ids with stable
placeholders, drops ad-serving metadata, and refuses to write anything that still looks like a
token. Product data is public and kept.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
DROP_KEYS = {"sponsoredCreative", "encryptedCost", "userData", "payment", "giftCardPayment", "wscMembershipPayment", "taxEntries"}
CUSTOMER_KEYS = {"customer": {"pcid": "", "name": "Test Customer", "email": "customer@example.invalid"}}
SECRET_RE = re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}|(?i:bearer\s+\S+)|(?i:\"(access|refresh|id)_?token\")")


def sanitize(obj, ids: dict[str, str]):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in DROP_KEYS:
                continue
            if k in CUSTOMER_KEYS:
                out[k] = CUSTOMER_KEYS[k]
                continue
            if k in ("orderContactNumber",):
                out[k] = "0000000000"
                continue
            out[k] = sanitize(v, ids)
        return out
    if isinstance(obj, list):
        return [sanitize(x, ids) for x in obj]
    if isinstance(obj, str):
        return UUID_RE.sub(lambda m: ids.setdefault(m.group(0), f"00000000-0000-4000-8000-{len(ids) + 1:012d}"), obj)
    return obj


def trim_tiles(obj, max_tiles: int):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "productTiles" and isinstance(v, list):
                obj[k] = v[:max_tiles]
            else:
                trim_tiles(v, max_tiles)
    elif isinstance(obj, list):
        for x in obj:
            trim_tiles(x, max_tiles)
    return obj


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--max-tiles", type=int, default=0)
    args = ap.parse_args()
    data = json.load(open(args.src))
    if args.max_tiles:
        data = trim_tiles(data, args.max_tiles)
    clean = sanitize(data, {})
    text = json.dumps(clean, indent=1, ensure_ascii=False)
    if SECRET_RE.search(text):
        sys.exit("Refusing to write: output still contains something token-like.")
    open(args.dst, "w").write(text + "\n")
    print(f"wrote {args.dst} ({len(text)} bytes)")


if __name__ == "__main__":
    main()
