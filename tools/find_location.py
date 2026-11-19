"""Look up MET forecast location ids by name / state-ish substring.

    python tools/find_location.py selangor
    python tools/find_location.py "kuala lumpur"
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data", "locations.json")


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    q = " ".join(sys.argv[1:]).lower()
    rows = json.load(open(DATA, encoding="utf-8"))
    hits = [r for r in rows if q in r["name"].lower()]
    if not hits:
        print(f"no location matching {q!r}")
        return
    for r in sorted(hits, key=lambda x: x["name"]):
        coords = f"{r['lat']:.4f},{r['lon']:.4f}" if r["lat"] is not None else "no coords"
        print(f"{r['id']:<8} {r['name']:<28} {coords}")


if __name__ == "__main__":
    main()
