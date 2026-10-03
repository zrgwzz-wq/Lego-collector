import argparse
import json

from services.catalog import (
    backfill_search_aliases,
    enforce_verified_korean_policy,
    sync_catalog,
    sync_status,
)
from services.kream import sync_watched
from lego_kr_sync import sync_current_shop, sync_instruction_names


def main():
    p = argparse.ArgumentParser(description="LEGO Collector catalog sync")
    p.add_argument("--max-calls", type=int, default=60)
    p.add_argument("--shop-pages", type=int, default=55)
    p.add_argument("--instruction-names", type=int, default=80)
    p.add_argument("--alias-batches", type=int, default=4)
    p.add_argument("--kream-limit", type=int, default=8)
    p.add_argument("--status", action="store_true")
    args = p.parse_args()

    if args.status:
        print(json.dumps(sync_status(), ensure_ascii=False, indent=2))
        return

    result = sync_catalog(args.max_calls)
    print(json.dumps({"catalog_sync": result}, ensure_ascii=False))

    shop = sync_current_shop(args.shop_pages)
    print(json.dumps({"lego_kr_shop": shop}, ensure_ascii=False))

    names = sync_instruction_names(args.instruction_names)
    print(json.dumps({"lego_kr_instruction_names": names}, ensure_ascii=False))

    policy = enforce_verified_korean_policy()
    print(json.dumps({"korean_name_policy": policy}, ensure_ascii=False))

    aliases = backfill_search_aliases(batch_size=1000, max_batches=max(1, args.alias_batches))
    print(json.dumps({"search_alias_backfill": aliases}, ensure_ascii=False))

    market = sync_watched(limit=max(1, args.kream_limit))
    print(json.dumps({"kream_watch_sync": market}, ensure_ascii=False))


if __name__ == "__main__":
    main()
