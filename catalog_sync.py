import argparse
import json
from services.catalog import sync_catalog, enforce_verified_korean_policy, sync_status

def main():
    p = argparse.ArgumentParser(description="LEGO Collector 2.0 catalog sync")
    p.add_argument("--max-calls", type=int, default=24)
    # 기존 GitHub Actions 명령과 호환하기 위해 남겨둡니다. 더 이상 제품명을 자동 번역하지 않습니다.
    p.add_argument("--koreanize", type=int, default=0)
    p.add_argument("--workers", type=int, default=0)
    p.add_argument("--status", action="store_true")
    args = p.parse_args()

    if args.status:
        print(json.dumps(sync_status(), ensure_ascii=False, indent=2))
        return

    result = sync_catalog(args.max_calls)
    print(json.dumps({"catalog_sync": result}, ensure_ascii=False))

    policy = enforce_verified_korean_policy()
    print(json.dumps({"korean_name_policy": policy}, ensure_ascii=False))

if __name__ == "__main__":
    main()
