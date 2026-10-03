import argparse
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from services.catalog import (
    MASTER_TABLE, ALIAS_TABLE, state_get, state_set,
    table_ready, upsert_aliases, _now_iso, _today
)
from services.supabase_client import configured, rest_get, rest_post

LEGO_KR = "https://www.lego.com"
SHOP_URL = "https://www.lego.com/ko-kr/categories/all-sets"
KR_CATALOG_TABLE = "lego_kr_catalog"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0 Mobile Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
}

PRICE_RE = re.compile(r'([0-9]{1,3}(?:,[0-9]{3})+)\s*원')
SET_RE = re.compile(r'-(\d{4,8})(?:[/?#]|$)')

def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s

def _clean_name(s):
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    s = re.sub(r'\s*[0-9]{1,3}(?:,[0-9]{3})+\s*원.*$', '', s).strip()
    return s

def _product_from_anchor(a):
    href = a.get("href") or ""
    if "/product/" not in href:
        return None
    m = SET_RE.search(href)
    if not m:
        return None
    set_number = m.group(1)

    name = _clean_name(" ".join(a.stripped_strings))
    bad = {"출시 예정", "장바구니 담기", "백오더", "품절", "신제품"}
    if not name or name in bad or len(name) > 180:
        for tag in ("h1", "h2", "h3", "h4"):
            h = a.find(tag)
            if h:
                candidate = _clean_name(" ".join(h.stripped_strings))
                if candidate and candidate not in bad:
                    name = candidate
                    break

    node = a
    price = None
    for _ in range(8):
        node = getattr(node, "parent", None)
        if node is None:
            break
        txt = " ".join(node.stripped_strings)
        if len(txt) > 5000:
            break
        pm = PRICE_RE.search(txt)
        if pm:
            try:
                price = int(pm.group(1).replace(",", ""))
            except Exception:
                price = None
            break

    if not name or name in bad:
        return None

    return {
        "set_number": set_number,
        "name_ko": name,
        "price_krw": price,
        "source_url": urljoin(LEGO_KR, href),
    }

def sync_current_shop(max_pages=55, force=False):
    if not configured() or not table_ready():
        return {"ok": False, "error": "supabase_not_ready"}

    if not force and state_get("lego_kr_shop_last_sync", None) == _today():
        return {"ok": True, "skipped": True, "reason": "already_synced_today"}

    s = _session()
    seen = {}
    pages_done = 0
    empty_pages = 0

    for page in range(1, max_pages + 1):
        url = SHOP_URL if page == 1 else f"{SHOP_URL}?page={page}"
        try:
            r = s.get(url, timeout=30)
            r.raise_for_status()
        except Exception as e:
            return {
                "ok": False, "error": f"lego_shop_fetch_failed:{type(e).__name__}",
                "page": page, "products_found": len(seen)
            }

        soup = BeautifulSoup(r.text, "html.parser")
        before = len(seen)

        for a in soup.find_all("a", href=True):
            item = _product_from_anchor(a)
            if not item:
                continue
            n = item["set_number"]
            prev = seen.get(n)
            # Prefer a row that has a KRW price.
            if prev is None or (prev.get("price_krw") is None and item.get("price_krw") is not None):
                seen[n] = item

        pages_done += 1
        if len(seen) == before:
            empty_pages += 1
        else:
            empty_pages = 0

        # LEGO PLP currently shows ~20-24 products per page.
        # Two consecutive pages with no new product links means we've reached the end.
        if empty_pages >= 2:
            break

        time.sleep(0.20)

    now = _now_iso()
    today = _today()
    master_rows = []
    kr_rows = []

    for item in seen.values():
        master_rows.append({
            "set_number": item["set_number"],
            "name_ko": item["name_ko"],
            "name_ko_source": "LEGO Korea 공식몰",
            "name_ko_updated_at": now,
            "price_krw": item.get("price_krw"),
            "updated_at": now,
        })
        kr_rows.append({
            "set_number": item["set_number"],
            "name_ko": item["name_ko"],
            "price_krw": item.get("price_krw"),
            "source": "LEGO Korea 공식몰",
            "source_url": item["source_url"],
            "checked_at": today,
            "updated_at": now,
        })

    ok = True
    for i in range(0, len(master_rows), 150):
        rr = rest_post(
            MASTER_TABLE, master_rows[i:i+150],
            {"on_conflict": "set_number"},
            timeout=30,
            prefer="resolution=merge-duplicates,return=minimal"
        )
        ok = ok and rr.ok

    for i in range(0, len(kr_rows), 150):
        rr = rest_post(
            KR_CATALOG_TABLE, kr_rows[i:i+150],
            {"on_conflict": "set_number"},
            timeout=30,
            prefer="resolution=merge-duplicates,return=minimal"
        )
        ok = ok and rr.ok

    if master_rows:
        upsert_aliases(master_rows)

    if ok:
        state_set("lego_kr_shop_last_sync", today)

    priced = sum(1 for x in seen.values() if x.get("price_krw") is not None)
    return {
        "ok": ok,
        "pages": pages_done,
        "products_found": len(seen),
        "prices_found": priced,
        "source": "LEGO Korea 공식몰",
    }

def _instruction_name(s, set_number):
    url = f"https://www.lego.com/ko-kr/service/building-instructions/{set_number}"
    try:
        r = s.get(url, timeout=20)
        if r.status_code != 200:
            return None
    except Exception:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    for h in soup.find_all("h1"):
        name = _clean_name(" ".join(h.stripped_strings))
        if not name:
            continue
        low = name.lower()
        if "조립 설명서" in name or "building instruction" in low:
            continue
        if len(name) > 180:
            continue
        return {"name_ko": name, "source_url": url}
    return None

def sync_instruction_names(limit=120):
    """Gradually add official Korean names for retired/older sets.
    No machine translation is used.
    """
    if not configured() or not table_ready():
        return {"ok": False, "error": "supabase_not_ready"}

    cursor = str(state_get("lego_kr_name_cursor", "") or "")
    params = {
        "select": "set_number",
        "name_ko": "is.null",
        "order": "set_number.asc",
        "limit": str(limit),
    }
    if cursor:
        params["set_number"] = f"gt.{cursor}"

    r = rest_get(MASTER_TABLE, params, timeout=20)
    if not r.ok:
        return {"ok": False, "error": "master_missing_name_query_failed"}

    rows = r.json() or []
    if not rows and cursor:
        state_set("lego_kr_name_cursor", "")
        return {"ok": True, "checked": 0, "found": 0, "wrapped": True}
    if not rows:
        return {"ok": True, "checked": 0, "found": 0}

    s = _session()
    found = []
    last = cursor

    for row in rows:
        n = str(row.get("set_number") or "")
        if not n:
            continue
        last = n
        item = _instruction_name(s, n)
        if item:
            found.append({
                "set_number": n,
                "name_ko": item["name_ko"],
                "name_ko_source": "LEGO Korea 조립 설명서",
                "name_ko_updated_at": _now_iso(),
                "updated_at": _now_iso(),
                "_source_url": item["source_url"],
            })
        time.sleep(0.15)

    master_payload = [{k:v for k,v in x.items() if not k.startswith("_")} for x in found]
    for i in range(0, len(master_payload), 100):
        rest_post(
            MASTER_TABLE, master_payload[i:i+100],
            {"on_conflict": "set_number"},
            timeout=25,
            prefer="resolution=merge-duplicates,return=minimal"
        )

    kr_payload = [{
        "set_number": x["set_number"],
        "name_ko": x["name_ko"],
        "source": "LEGO Korea 조립 설명서",
        "source_url": x["_source_url"],
        "checked_at": _today(),
        "updated_at": _now_iso(),
    } for x in found]

    for i in range(0, len(kr_payload), 100):
        rest_post(
            KR_CATALOG_TABLE, kr_payload[i:i+100],
            {"on_conflict": "set_number"},
            timeout=25,
            prefer="resolution=merge-duplicates,return=minimal"
        )

    if master_payload:
        upsert_aliases(master_payload)

    state_set("lego_kr_name_cursor", last)

    return {
        "ok": True,
        "checked": len(rows),
        "found": len(found),
        "cursor": last,
        "source": "LEGO Korea 조립 설명서",
    }

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--shop-pages", type=int, default=55)
    p.add_argument("--instruction-names", type=int, default=120)
    p.add_argument("--force-shop", action="store_true")
    args = p.parse_args()

    print(json.dumps(
        {"lego_kr_shop": sync_current_shop(args.shop_pages, args.force_shop)},
        ensure_ascii=False
    ))
    print(json.dumps(
        {"lego_kr_instruction_names": sync_instruction_names(args.instruction_names)},
        ensure_ascii=False
    ))

if __name__ == "__main__":
    main()
