import argparse
import json
from services.catalog import sync_catalog, enforce_verified_korean_policy, sync_status
from lego_kr_sync import sync_current_shop, sync_instruction_names


def main():
    p = argparse.ArgumentParser(description="LEGO Collector catalog sync")
    p.add_argument("--max-calls", type=int, default=60)
    p.add_argument("--shop-pages", type=int, default=55)
    p.add_argument("--instruction-names", type=int, default=80)
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


if __name__ == "__main__":
    main()
