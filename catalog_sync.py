import os, json
from server import run_master_sync, run_bricklink_enrich, bricklink_configured
max_calls=int(os.getenv("CATALOG_SYNC_MAX_CALLS","20"))
result=run_master_sync(max_calls)
print(json.dumps(result,ensure_ascii=False))
if result.get("ok") and result.get("initial_complete") and bricklink_configured():
    print(json.dumps(run_bricklink_enrich(int(os.getenv("BRICKLINK_SYNC_MAX_ITEMS","20"))),ensure_ascii=False))
