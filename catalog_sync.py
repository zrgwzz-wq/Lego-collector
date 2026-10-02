
import argparse
import json
from services.catalog import sync_catalog, koreanize_missing, sync_status

def main():
    p = argparse.ArgumentParser(description="LEGO Collector 2.0 catalog sync")
    p.add_argument("--max-calls", type=int, default=24)
    p.add_argument("--koreanize", type=int, default=400)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--status", action="store_true")
    args = p.parse_args()

    if args.status:
        print(json.dumps(sync_status(), ensure_ascii=False, indent=2))
        return

    result = sync_catalog(args.max_calls)
    print(json.dumps({"catalog_sync": result}, ensure_ascii=False))
    ko = koreanize_missing(args.koreanize, args.workers)
    print(json.dumps({"koreanize": ko}, ensure_ascii=False))

if __name__ == "__main__":
    main()
