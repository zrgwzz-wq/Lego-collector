
import json
import math
import os
import re
import threading
import time
from pathlib import Path

import requests

from .supabase_client import (
    configured, rest_get, rest_post, rest_delete, rpc, service_headers
)

BRICKSET_API = "https://brickset.com/api/v3.asmx"
BRICKSET_API_KEY = os.getenv("BRICKSET_API_KEY", "")
MASTER_TABLE = "lego_master_catalog"
ALIAS_TABLE = "lego_product_aliases"
STATE_TABLE = "lego_catalog_sync_state"

FILTER_CACHE = {"t": 0, "data": None}
LOCAL_KR = None
SYNC_LOCK = threading.Lock()

AUTO_TRANSLATION_MARKERS = ("자동 번역", "machine translation", "google translate", "auto translate")

# 화면에 표시할 제품명을 번역하는 사전이 아닙니다.
# 영문 제품명/테마에 이 단어가 실제로 포함될 때 한국어 검색어만 추가합니다.
CURATED_SEARCH_TERMS = {
    "star wars": ["스타워즈"],
    "marvel": ["마블"],
    "spider-man": ["스파이더맨"],
    "spiderman": ["스파이더맨"],
    "spidey": ["스파이디"],
    "iron man": ["아이언맨"],
    "avengers": ["어벤져스"],
    "captain america": ["캡틴 아메리카"],
    "hulk": ["헐크"],
    "thor": ["토르"],
    "batman": ["배트맨"],
    "superman": ["슈퍼맨"],
    "harry potter": ["해리 포터", "해리포터"],
    "lord of the rings": ["반지의 제왕"],
    "super mario": ["슈퍼 마리오", "슈퍼마리오"],
    "minecraft": ["마인크래프트"],
    "jurassic world": ["쥬라기 월드", "주라기 월드"],
    "disney": ["디즈니"],
    "technic": ["테크닉"],
    "speed champions": ["스피드 챔피언"],
    "architecture": ["아키텍처"],
    "ninjago": ["닌자고"],
    "duplo": ["듀플로"],
    "friends": ["프렌즈"],
    "sanctum": ["생텀"],
    "sanctorum": ["생토럼"],
    "ferrari": ["페라리"],
    "lamborghini": ["람보르기니"],
    "porsche": ["포르쉐"],
    "mclaren": ["맥라렌"],
    "mercedes": ["메르세데스", "벤츠"],
}

def _is_auto_translation_source(source):
    s = str(source or "").strip().lower()
    return any(marker.lower() in s for marker in AUTO_TRANSLATION_MARKERS)

def _curated_search_aliases(row):
    hay = " ".join([
        str(row.get("name_en") or ""),
        str(row.get("theme") or ""),
        str(row.get("subtheme") or ""),
    ]).lower()
    aliases = []
    for english, korean_terms in CURATED_SEARCH_TERMS.items():
        if english in hay:
            aliases.extend(korean_terms)
    return sorted(set(a for a in aliases if a))

def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())

def _load_local_kr():
    global LOCAL_KR
    if LOCAL_KR is not None:
        return LOCAL_KR
    path = Path(__file__).resolve().parent.parent / "kr_catalog.json"
    try:
        LOCAL_KR = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        LOCAL_KR = {}
    return LOCAL_KR

def table_ready(table=MASTER_TABLE):
    if not configured():
        return False
    try:
        r = rest_get(table, {"select": "set_number", "limit": "1"}, timeout=4)
        return r.ok
    except Exception:
        return False

def state_get(key, default=None):
    if not configured():
        return default
    try:
        r = rest_get(STATE_TABLE, {"select": "value", "key": f"eq.{key}", "limit": "1"}, timeout=5)
        if r.ok and r.json():
            return r.json()[0].get("value", default)
    except Exception:
        pass
    return default

def state_set(key, value):
    if not configured():
        return False
    payload = {"key": key, "value": value, "updated_at": _now_iso()}
    try:
        r = rest_post(
            STATE_TABLE, payload,
            {"on_conflict": "key"},
            timeout=8,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        return r.ok
    except Exception:
        return False

def catalog_count():
    if not table_ready():
        return 0
    try:
        h = service_headers("count=exact")
        r = rest_get(MASTER_TABLE, {"select": "set_number", "limit": "1"}, timeout=6, headers=h)
        cr = r.headers.get("Content-Range", "")
        tail = cr.rsplit("/", 1)[-1] if "/" in cr else ""
        return int(tail) if tail.isdigit() else 0
    except Exception:
        return 0

def search_catalog(q="", theme="", subtheme="", year=None, category="", sort="newest", page=1, page_size=40, released=True):
    if not table_ready():
        return {"ok": False, "error": "master_table_missing", "items": [], "total": 0, "pages": 1, "page": 1}
    try:
        page = max(1, int(page))
        page_size = min(60, max(10, int(page_size)))
    except Exception:
        page, page_size = 1, 40
    payload = {
        "p_q": (q or "").strip(),
        "p_theme": (theme or "").strip() or None,
        "p_subtheme": (subtheme or "").strip() or None,
        "p_year": int(year) if str(year or "").isdigit() else None,
        "p_category": (category or "").strip() or None,
        "p_released": bool(released),
        "p_sort": sort or "newest",
        "p_offset": (page - 1) * page_size,
        "p_limit": page_size,
    }
    try:
        r = rpc("search_lego_catalog", payload, timeout=12)
        if not r.ok:
            return {"ok": False, "error": "catalog_query_failed", "detail": r.text[:300], "items": [], "total": 0, "pages": 1, "page": page}
        rows = r.json() or []
        total = int(rows[0].get("total_count") or 0) if rows else 0
        for x in rows:
            x.pop("total_count", None)
        return {
            "ok": True, "items": rows, "total": total, "page": page,
            "page_size": page_size, "pages": max(1, math.ceil(total / page_size)) if total else 1
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "items": [], "total": 0, "pages": 1, "page": page}

def get_set(set_number):
    n = str(set_number or "").strip().split("-")[0]
    if not n or not table_ready():
        return None
    cols = (
        "set_number,brickset_number,name_en,name_ko,name_ko_source,theme,subtheme,category,year,pieces,minifigs,"
        "released,image_url,thumbnail_url,brickset_url,price_krw,retail_us,retail_uk,retail_de,launch_date,exit_date,availability,description,"
        "bricklink_number,bricklink_name,bricklink_alt_no,bricklink_image_url"
    )
    try:
        r = rest_get(MASTER_TABLE, {"select": cols, "set_number": f"eq.{n}", "limit": "1"}, timeout=8)
        if r.ok and r.json():
            row = r.json()[0]
            # Read existing KREAM history only; browsing never calls KREAM.
            try:
                t = rest_get(
                    "lego_kream_trades",
                    {"select": "price_krw,trade_at,source_url", "set_number": f"eq.{n}", "order": "trade_at.desc", "limit": "2000"},
                    timeout=8
                )
                if t.ok:
                    trades = t.json() or []
                    row["kream_trades"] = trades
                    if trades:
                        row["kream_latest"] = trades[0]
            except Exception:
                row["kream_trades"] = []
            return row
    except Exception:
        pass
    return None

def facets():
    now = time.time()
    if FILTER_CACHE.get("data") and now - FILTER_CACHE.get("t", 0) < 1800:
        return FILTER_CACHE["data"]
    data = {"themes": [], "subthemes": [], "years": [], "categories": []}
    if not table_ready():
        return data
    try:
        r = rpc("lego_catalog_facets", {}, timeout=12)
        if r.ok:
            raw = r.json() or {}
            if isinstance(raw, dict):
                data = {
                    "themes": raw.get("themes") or [],
                    "subthemes": raw.get("subthemes") or [],
                    "years": raw.get("years") or [],
                    "categories": raw.get("categories") or [],
                }
    except Exception:
        pass
    FILTER_CACHE.update({"t": now, "data": data})
    return data

def _brickset_call(method, params, timeout=20):
    if not BRICKSET_API_KEY:
        raise RuntimeError("BRICKSET_API_KEY not configured")
    payload = {"apiKey": BRICKSET_API_KEY}
    if method == "getSets":
        payload["userHash"] = ""
    payload.update(params or {})
    r = requests.get(f"{BRICKSET_API}/{method}", params=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if str(data.get("status", "")).lower() not in ("success", ""):
        raise RuntimeError(data.get("message") or "Brickset API error")
    return data

def _years():
    try:
        d = _brickset_call("getYears", {"theme": ""})
        years = [int(x.get("year")) for x in d.get("years", []) if str(x.get("year") or "").isdigit()]
        return sorted(set(years), reverse=True)
    except Exception:
        # Wide fallback, actual empty years simply return zero matches.
        current = int(time.strftime("%Y"))
        return list(range(current + 1, 1948, -1))

def _verified_overlays():
    out = {}
    for n, row in (_load_local_kr() or {}).items():
        if not isinstance(row, dict):
            continue
        source = row.get("source") or "기존 검증 한국 카탈로그"
        name_ko = None if _is_auto_translation_source(source) else row.get("name_ko")
        out[str(n)] = {
            "name_ko": name_ko,
            "price_krw": row.get("price"),
            "source": source if name_ko else None,
        }
    if configured():
        try:
            r = rest_get("lego_kr_catalog", {"select": "set_number,name_ko,price_krw,source", "limit": "10000"}, timeout=12)
            if r.ok:
                for row in r.json() or []:
                    n = str(row.get("set_number") or "")
                    if not n:
                        continue
                    prev = out.get(n, {})
                    source = row.get("source") or prev.get("source") or "기존 검증 한국 카탈로그"
                    candidate_name = row.get("name_ko")
                    if _is_auto_translation_source(source):
                        candidate_name = None
                    out[n] = {
                        "name_ko": candidate_name or prev.get("name_ko"),
                        "price_krw": row.get("price_krw") if row.get("price_krw") is not None else prev.get("price_krw"),
                        "source": source if candidate_name else prev.get("source"),
                    }
        except Exception:
            pass
    return out

PLACEHOLDERS = {"{?}", "?", "TBA", "TBD", "UNKNOWN", "UNNAMED SET"}

def _real_named_item(item):
    name = str((item or {}).get("name") or "").strip()
    return bool(name and name.upper() not in PLACEHOLDERS)

def _row_from_brickset(item, overlays):
    full = str(item.get("number") or "")
    n = full.split("-")[0]
    image = item.get("image") or {}
    lego = item.get("LEGOCom") or {}
    ext = item.get("extendedData") or {}
    ov = overlays.get(n, {})
    def retail(region):
        x = lego.get(region) or {}
        return x.get("retailPrice")
    row = {
        "set_number": n,
        "brickset_number": full,
        "set_id": item.get("setID"),
        "name_en": item.get("name"),
        # name_ko / price_krw는 Brickset 동기화가 덮어쓰지 않습니다.
        # 한국명/한국 정가는 별도 overlay 작업에서만 갱신합니다.
        "theme": item.get("theme"),
        "subtheme": item.get("subtheme"),
        "category": item.get("category"),
        "year": item.get("year"),
        "pieces": item.get("pieces"),
        "minifigs": item.get("minifigs"),
        "released": item.get("released"),
        "image_url": image.get("imageURL"),
        "thumbnail_url": image.get("thumbnailURL"),
        "brickset_url": item.get("bricksetURL"),
        "retail_us": retail("US"),
        "retail_uk": retail("UK"),
        "retail_de": retail("DE"),
        "launch_date": item.get("launchDate"),
        "exit_date": item.get("exitDate"),
        "availability": item.get("availability"),
        "description": ext.get("description"),
        "brickset_last_updated": item.get("lastUpdated"),
        "updated_at": _now_iso(),
    }
    return row

def upsert_master(rows):
    if not rows:
        return True
    ok = True
    for i in range(0, len(rows), 200):
        try:
            r = rest_post(
                MASTER_TABLE, rows[i:i+200], {"on_conflict": "set_number"},
                timeout=25, prefer="resolution=merge-duplicates,return=minimal"
            )
            ok = ok and r.ok
        except Exception:
            ok = False
    FILTER_CACHE["t"] = 0
    return ok

def _normalize_alias(s):
    return re.sub(r"[^0-9a-z가-힣]+", "", str(s or "").lower())

def upsert_aliases(rows):
    payload = []
    now = _now_iso()
    for row in rows:
        n = str(row.get("set_number") or "")
        if not n:
            continue

        name_ko = row.get("name_ko")
        name_ko_source = row.get("name_ko_source") or "catalog"
        if name_ko and not _is_auto_translation_source(name_ko_source):
            norm = _normalize_alias(name_ko)
            if norm:
                payload.append({
                    "alias": str(name_ko), "alias_normalized": norm,
                    "set_number": n, "alias_type": "korean_name",
                    "source": name_ko_source, "updated_at": now
                })

        for alias, kind, source in [
            (row.get("name_en"), "english_name", "Brickset"),
            (row.get("bricklink_name"), "bricklink_name", "BrickLink"),
            (row.get("bricklink_alt_no"), "alternate_number", "BrickLink"),
        ]:
            if not alias:
                continue
            norm = _normalize_alias(alias)
            if norm:
                payload.append({
                    "alias": str(alias), "alias_normalized": norm,
                    "set_number": n, "alias_type": kind, "source": source,
                    "updated_at": now
                })

        for alias in _curated_search_aliases(row):
            norm = _normalize_alias(alias)
            if norm:
                payload.append({
                    "alias": alias, "alias_normalized": norm,
                    "set_number": n, "alias_type": "curated_search_term",
                    "source": "내부 검색 사전", "updated_at": now
                })

    if not payload:
        return True
    ok = True
    for i in range(0, len(payload), 300):
        try:
            r = rest_post(
                ALIAS_TABLE, payload[i:i+300],
                {"on_conflict": "alias_normalized,set_number"},
                timeout=20, prefer="resolution=merge-duplicates,return=minimal"
            )
            ok = ok and r.ok
        except Exception:
            ok = False
    return ok

def sync_catalog(max_calls=24):
    if not configured():
        return {"ok": False, "error": "supabase_not_configured"}
    if not table_ready():
        return {"ok": False, "error": "master_table_missing"}
    if not BRICKSET_API_KEY:
        return {"ok": False, "error": "brickset_key_missing"}
    if not SYNC_LOCK.acquire(blocking=False):
        return {"ok": True, "already_running": True}

    calls = 0
    saved = 0
    overlays = _verified_overlays()
    try:
        # 표시용 한글명은 검증된 이름만 유지. 자동 번역명은 제거합니다.
        enforce_verified_korean_policy()
        initial_complete = bool(state_get("initial_complete", False))
        if not initial_complete:
            years = state_get("years", None)
            if not isinstance(years, list) or not years:
                years = _years()
                state_set("years", years)
            yi = int(state_get("year_index", 0) or 0)
            page = int(state_get("page", 1) or 1)

            while calls < max_calls and yi < len(years):
                year = years[yi]
                d = _brickset_call("getSets", {
                    "params": json.dumps({
                        "year": year, "pageSize": 500, "pageNumber": page,
                        "extendedData": 1, "orderBy": "Number"
                    })
                })
                calls += 1
                sets = d.get("sets", []) or []
                rows = [_row_from_brickset(x, overlays) for x in sets if x.get("number") and _real_named_item(x)]
                if rows:
                    upsert_master(rows)
                    upsert_aliases(rows)
                    saved += len(rows)
                matches = int(d.get("matches") or len(sets))
                pages = max(1, math.ceil(matches / 500))
                if page >= pages:
                    yi += 1
                    page = 1
                else:
                    page += 1
                state_set("year_index", yi)
                state_set("page", page)
                state_set("last_progress", {"year": year, "page": page, "calls": calls, "rows_saved": saved})

            done = yi >= len(years)
            if done:
                state_set("initial_complete", True)
                state_set("last_sync_date", _today())
            return {"ok": True, "mode": "initial", "calls": calls, "rows_saved": saved, "initial_complete": done}

        last = str(state_get("last_sync_date", _today()) or _today())
        page = 1
        while calls < max_calls:
            d = _brickset_call("getSets", {
                "params": json.dumps({
                    "updatedSince": last, "pageSize": 500, "pageNumber": page,
                    "extendedData": 1, "orderBy": "Number"
                })
            })
            calls += 1
            sets = d.get("sets", []) or []
            rows = [_row_from_brickset(x, overlays) for x in sets if x.get("number") and _real_named_item(x)]
            if rows:
                upsert_master(rows)
                upsert_aliases(rows)
                saved += len(rows)
            matches = int(d.get("matches") or len(sets))
            pages = max(1, math.ceil(matches / 500))
            if page >= pages:
                break
            page += 1
        state_set("last_sync_date", _today())
        return {"ok": True, "mode": "incremental", "calls": calls, "rows_saved": saved, "initial_complete": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300], "calls": calls, "rows_saved": saved}
    finally:
        SYNC_LOCK.release()

def remove_auto_translated_display_names(max_batches=20):
    """Remove only names explicitly marked as automatic translations.
    Verified/official Korean names are never touched.
    """
    if not table_ready():
        return {"ok": False, "error": "master_table_missing", "cleared": 0}

    cleared = 0
    ok = True
    for _ in range(max_batches):
        try:
            r = rest_get(
                MASTER_TABLE,
                {
                    "select": "set_number,name_ko_source",
                    "name_ko_source": "eq.자동 번역",
                    "limit": "500",
                },
                timeout=12,
            )
            rows = r.json() if r.ok else []
        except Exception:
            rows = []
        if not rows:
            break

        patches = [{
            "set_number": str(x.get("set_number")),
            "name_ko": None,
            "name_ko_source": None,
            "name_ko_updated_at": None,
            "updated_at": _now_iso(),
        } for x in rows if x.get("set_number")]
        if patches:
            ok = upsert_master(patches) and ok
            cleared += len(patches)

    try:
        rest_delete(ALIAS_TABLE, {"source": "eq.자동 번역"}, timeout=12)
    except Exception:
        pass

    return {"ok": ok, "cleared": cleared}

def apply_verified_korean():
    overlays = _verified_overlays()
    rows = []
    now = _now_iso()
    for n, ov in overlays.items():
        if not ov.get("name_ko") and ov.get("price_krw") is None:
            continue
        row = {"set_number": n, "updated_at": now}
        if ov.get("name_ko"):
            row.update({
                "name_ko": ov.get("name_ko"),
                "name_ko_source": ov.get("source") or "검증 한국명",
                "name_ko_updated_at": now,
            })
        if ov.get("price_krw") is not None:
            row["price_krw"] = ov.get("price_krw")
        rows.append(row)
    ok = upsert_master(rows) if rows else True
    upsert_aliases(rows)
    return {"ok": ok, "updated": len(rows)}

def enforce_verified_korean_policy():
    cleanup = remove_auto_translated_display_names()
    verified = apply_verified_korean()
    return {
        "ok": bool(cleanup.get("ok")) and bool(verified.get("ok")),
        "auto_names_cleared": cleanup.get("cleared", 0),
        "verified_names_applied": verified.get("updated", 0),
        "policy": "verified_korean_else_english",
    }

def sync_status():
    return {
        "ok": True,
        "table_ready": table_ready(),
        "count": catalog_count(),
        "initial_complete": bool(state_get("initial_complete", False)),
        "progress": state_get("last_progress", {}),
        "last_sync_date": state_get("last_sync_date", None),
    }
