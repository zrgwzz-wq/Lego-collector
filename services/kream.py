import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests

from .supabase_client import configured, rest_get, rest_post, rest_patch

TRADES_TABLE = "lego_kream_trades"
WATCH_TABLE = "lego_kream_watch"
MAX_HISTORY_PAGES = 40

# LEGO set number -> alternate model number used on KREAM.
MODEL_ALIASES = {
    "5009609": ["6601584"],
}


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _headers(referer=None, accept_json=False):
    h = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/143 Mobile Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
    }
    if accept_json:
        h["Accept"] = "application/json, text/plain, */*"
    if referer:
        h["Referer"] = referer
    return h


def _table_ready(table):
    if not configured():
        return False
    try:
        r = rest_get(table, {"select": "set_number", "limit": "1"}, timeout=5)
        return r.ok
    except Exception:
        return False


def watch_ready():
    return _table_ready(WATCH_TABLE)


def trades_ready():
    return _table_ready(TRADES_TABLE)


def _clean_number(value):
    return re.sub(r"[^0-9]", "", str(value or ""))


def watch_many(numbers):
    """Register set numbers for shared background market refresh.

    No user identity is stored; only the LEGO set number and timestamps are kept.
    """
    if not watch_ready():
        return {"ok": False, "error": "watch_table_missing", "watched": 0}
    now = _now_iso()
    clean = []
    seen = set()
    for value in numbers or []:
        n = _clean_number(value)
        if not n or n in seen:
            continue
        seen.add(n)
        clean.append({"set_number": n, "last_requested_at": now, "updated_at": now})
        if len(clean) >= 100:
            break
    if not clean:
        return {"ok": True, "watched": 0}
    try:
        r = rest_post(
            WATCH_TABLE,
            clean,
            {"on_conflict": "set_number"},
            timeout=20,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        return {"ok": bool(r.ok), "watched": len(clean) if r.ok else 0}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "watched": 0}


def watch_one(number):
    return watch_many([number])


def _parse_iso_utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _kst_display(value):
    dt = _parse_iso_utc(value)
    if not dt:
        return None
    return dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")


def _fallback_trade_time(text):
    raw = str(text or "").strip()
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{2})", raw)
    if m:
        try:
            return (
                datetime(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, 0, tzinfo=kst)
                .astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except Exception:
            return None
    m = re.fullmatch(r"(\d+)\s*분\s*전", raw)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    m = re.fullmatch(r"(\d+)\s*시간\s*전", raw)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    m = re.fullmatch(r"(\d+)\s*일\s*전", raw)
    if m:
        return (now - timedelta(days=int(m.group(1)))).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def _decode_jsonish(text):
    if not text:
        return ""
    try:
        text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    except Exception:
        pass
    return text.replace("\\/", "/").replace('\\"', '"')


def _bad_page_text(value):
    s = str(value or "").lower()
    return any(x in s for x in ("access denied", "forbidden", "captcha", "cloudflare", "robot check"))


def _extract_detail_links(html, base, allowed_host="kream.co.kr"):
    links = []
    for href in re.findall(r"href=[\\\"']([^\\\"']+)[\\\"']", html or "", re.I):
        u = urljoin(base, href.replace("&amp;", "&"))
        try:
            host = urlparse(u).netloc.lower()
        except Exception:
            continue
        if allowed_host not in host:
            continue
        if "/products/" in u.lower() and u not in links:
            links.append(u)
        if len(links) >= 12:
            break
    return links


def _watch_context(number):
    if not watch_ready():
        return None
    n = _clean_number(number)
    try:
        r = rest_get(
            WATCH_TABLE,
            {
                "select": "set_number,kream_product_id,model_number,source_url,last_synced_at,last_status",
                "set_number": f"eq.{n}",
                "limit": "1",
            },
            timeout=8,
        )
        rows = r.json() if r.ok else []
        if rows and rows[0].get("kream_product_id"):
            row = rows[0]
            return {
                "number": n,
                "model_number": row.get("model_number") or n,
                "product_id": int(row.get("kream_product_id")),
                "source_url": row.get("source_url") or f"https://kream.co.kr/products/{row.get('kream_product_id')}",
                "html": "",
            }
    except Exception:
        pass
    return None


def _product_context_requests(number):
    n = _clean_number(number)
    accepted = [n] + [x for x in MODEL_ALIASES.get(n, []) if x != n]
    for query_number in accepted:
        search = f"https://kream.co.kr/search?keyword={query_number}"
        try:
            r = requests.get(search, headers=_headers(), timeout=12)
            if not r.ok or _bad_page_text((r.text or "")[:5000]):
                continue
            for u in _extract_detail_links(r.text, r.url)[:8]:
                try:
                    d = requests.get(u, headers=_headers(u), timeout=12)
                    if not d.ok:
                        continue
                    text = _decode_jsonish(d.text or "")
                    matched = next(
                        (x for x in accepted if re.search(rf"(?<!\d){re.escape(x)}(?!\d)", text)),
                        None,
                    )
                    if not matched:
                        continue
                    pm = re.search(r"/products/(\d+)", d.url or u)
                    if not pm:
                        pm = re.search(r'"productID"\s*:\s*"?(\d+)"?', text, re.I)
                    if not pm:
                        pm = re.search(r'"product_id"\s*:\s*(\d+)', text, re.I)
                    if not pm:
                        continue
                    return {
                        "number": n,
                        "model_number": matched,
                        "product_id": int(pm.group(1)),
                        "source_url": d.url or u,
                        "html": d.text or "",
                        "method": "requests",
                    }
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _product_context_browser(number):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None

    n = _clean_number(number)
    accepted = [n] + [x for x in MODEL_ALIASES.get(n, []) if x != n]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(locale="ko-KR", user_agent=_headers()["User-Agent"])
            page = context.new_page()
            for query_number in accepted:
                search = f"https://kream.co.kr/search?keyword={query_number}"
                try:
                    page.goto(search, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1000)
                    links = page.eval_on_selector_all(
                        'a[href*="/products/"]',
                        "els => [...new Set(els.map(x => x.href || x.getAttribute('href')).filter(Boolean))].slice(0,10)",
                    )
                except Exception:
                    links = []
                for u in links or []:
                    try:
                        page.goto(u, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(600)
                        body = page.locator("body").inner_text(timeout=5000)
                        matched = next(
                            (x for x in accepted if re.search(rf"(?<!\d){re.escape(x)}(?!\d)", body or "")),
                            None,
                        )
                        if not matched:
                            continue
                        pm = re.search(r"/products/(\d+)", page.url)
                        if not pm:
                            continue
                        html = page.content()
                        result = {
                            "number": n,
                            "model_number": matched,
                            "product_id": int(pm.group(1)),
                            "source_url": page.url,
                            "html": html,
                            "method": "playwright",
                        }
                        browser.close()
                        return result
                    except Exception:
                        continue
            browser.close()
    except Exception:
        return None
    return None


def product_context(number, allow_browser=True):
    cached = _watch_context(number)
    if cached:
        return cached
    ctx = _product_context_requests(number)
    if ctx:
        return ctx
    if allow_browser:
        return _product_context_browser(number)
    return None


def _latest_stored_trade_at(number):
    if not trades_ready():
        return None
    n = _clean_number(number)
    try:
        r = rest_get(
            TRADES_TABLE,
            {"set_number": f"eq.{n}", "select": "trade_at", "order": "trade_at.desc", "limit": "1"},
            timeout=8,
        )
        rows = r.json() if r.ok else []
        return rows[0].get("trade_at") if rows else None
    except Exception:
        return None


def _public_html_trades(ctx):
    text = _decode_jsonish(ctx.get("html") or "")
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"&nbsp;", " ", plain, flags=re.I)
    plain = re.sub(r"\s+", " ", plain)
    pos = plain.find("체결 거래")
    if pos < 0:
        return []
    area = plain[pos : pos + 8000]
    rows = []
    for m in re.finditer(
        r"([1-9][0-9]{0,2}(?:,[0-9]{3})+)\s*원\s*((?:\d{2}/\d{2}/\d{2})|(?:\d+\s*(?:분|시간|일)\s*전))",
        area,
    ):
        try:
            price = int(m.group(1).replace(",", ""))
        except Exception:
            continue
        trade_at = _fallback_trade_time(m.group(2))
        if not trade_at:
            continue
        rows.append(
            {
                "set_number": ctx["number"],
                "kream_product_id": ctx["product_id"],
                "model_number": ctx.get("model_number"),
                "price_krw": price,
                "option_name": "ONE SIZE",
                "trade_at": trade_at,
                "source_url": ctx.get("source_url"),
            }
        )
    rows.sort(key=lambda x: x["trade_at"], reverse=True)
    return rows


def _sales_page_requests(ctx, cursor):
    pid = ctx["product_id"]
    detail = ctx.get("source_url") or f"https://kream.co.kr/products/{pid}"
    r = requests.get(
        f"https://kream.co.kr/api/p/products/{pid}/sales",
        params={"cursor": cursor, "per_page": 50, "sort": "date_created[desc]"},
        headers=_headers(detail, True),
        timeout=12,
    )
    if not r.ok:
        raise RuntimeError(f"http {r.status_code}")
    return r.json() or {}


def _fetch_sales_browser(ctx, stop_at=None, full_history=True):
    """Fetch KREAM sales through one same-origin Chromium session."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return [], False, False

    pid = ctx["product_id"]
    detail = ctx.get("source_url") or f"https://kream.co.kr/products/{pid}"
    rows = []
    cursor = 1
    pages = 0
    complete = True

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(locale="ko-KR", user_agent=_headers()["User-Agent"])
            page = context.new_page()
            page.goto(detail, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(600)

            while cursor and pages < MAX_HISTORY_PAGES:
                pages += 1
                data = page.evaluate(
                    """async ({pid,cursor}) => {
                        const u = `/api/p/products/${pid}/sales?cursor=${encodeURIComponent(cursor)}&per_page=50&sort=date_created%5Bdesc%5D`;
                        const r = await fetch(u, {credentials:'include', headers:{'Accept':'application/json, text/plain, */*'}});
                        if (!r.ok) return {__error:r.status};
                        return await r.json();
                    }""",
                    {"pid": pid, "cursor": cursor},
                )
                if not isinstance(data, dict) or data.get("__error"):
                    complete = False
                    break
                items = data.get("items") or []
                if not items:
                    break
                reached_old = False
                for item in items:
                    trade_at = item.get("date_created")
                    dt = _parse_iso_utc(trade_at)
                    if not dt:
                        continue
                    if stop_at and dt <= stop_at:
                        reached_old = True
                        continue
                    try:
                        price = int(round(float(item.get("price") or 0)))
                    except Exception:
                        price = 0
                    if price < 1000 or price > 10000000:
                        continue
                    option = str(item.get("option") or ((item.get("product_option") or {}).get("name_display")) or "")
                    rows.append({
                        "set_number": ctx["number"],
                        "kream_product_id": pid,
                        "model_number": ctx.get("model_number"),
                        "price_krw": price,
                        "option_name": option,
                        "trade_at": trade_at,
                        "source_url": detail,
                    })
                if reached_old:
                    break
                nxt = data.get("next_cursor")
                if not full_history or not nxt:
                    break
                cursor = nxt

            if pages >= MAX_HISTORY_PAGES and cursor:
                complete = False
            browser.close()
    except Exception:
        return [], False, False

    rows.sort(key=lambda x: str(x.get("trade_at") or ""), reverse=True)
    return rows, complete, True


def fetch_sales(ctx, full_history=True):
    stop_at = _parse_iso_utc(_latest_stored_trade_at(ctx["number"]))
    rows = []
    complete = True
    used_api = False
    used_browser = False
    cursor = 1
    pages = 0

    while cursor and pages < MAX_HISTORY_PAGES:
        pages += 1
        try:
            data = _sales_page_requests(ctx, cursor)
            used_api = True
        except Exception:
            # If the public API is blocked, use a single browser session for the
            # entire remaining history instead of launching Chromium per page.
            if not rows:
                browser_rows, browser_complete, browser_ok = _fetch_sales_browser(
                    ctx, stop_at=stop_at, full_history=full_history
                )
                if browser_ok:
                    rows = browser_rows
                    complete = browser_complete
                    used_api = True
                    used_browser = True
                else:
                    complete = False
                    rows = _public_html_trades(ctx)
            else:
                complete = False
            break

        items = data.get("items") or []
        if not items:
            break

        reached_old = False
        for item in items:
            trade_at = item.get("date_created")
            dt = _parse_iso_utc(trade_at)
            if not dt:
                continue
            if stop_at and dt <= stop_at:
                reached_old = True
                continue
            try:
                price = int(round(float(item.get("price") or 0)))
            except Exception:
                price = 0
            if price < 1000 or price > 10000000:
                continue
            option = str(item.get("option") or ((item.get("product_option") or {}).get("name_display")) or "")
            rows.append({
                "set_number": ctx["number"],
                "kream_product_id": ctx["product_id"],
                "model_number": ctx.get("model_number"),
                "price_krw": price,
                "option_name": option,
                "trade_at": trade_at,
                "source_url": ctx.get("source_url"),
            })

        if reached_old:
            break
        nxt = data.get("next_cursor")
        if not full_history or not nxt:
            break
        cursor = nxt

    if pages >= MAX_HISTORY_PAGES and cursor:
        complete = False
    rows.sort(key=lambda x: str(x.get("trade_at") or ""), reverse=True)
    return rows, complete, used_api, used_browser


def upsert_trades(rows):
    if not rows or not trades_ready():
        return 0
    saved = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i : i + 200]
        try:
            r = rest_post(
                TRADES_TABLE,
                chunk,
                {"on_conflict": "set_number,kream_product_id,trade_at,price_krw,option_name"},
                timeout=20,
                prefer="resolution=merge-duplicates,return=minimal",
            )
            if r.ok:
                saved += len(chunk)
        except Exception:
            pass
    return saved


def stored_trades(number, limit=5000):
    if not trades_ready():
        return []
    n = _clean_number(number)
    try:
        r = rest_get(
            TRADES_TABLE,
            {
                "set_number": f"eq.{n}",
                "select": "set_number,kream_product_id,model_number,price_krw,option_name,trade_at,source_url",
                "order": "trade_at.asc",
                "limit": str(min(max(int(limit), 1), 10000)),
            },
            timeout=15,
        )
        return r.json() if r.ok else []
    except Exception:
        return []


def _patch_watch(number, **fields):
    if not watch_ready():
        return False
    n = _clean_number(number)
    fields["updated_at"] = _now_iso()
    try:
        r = rest_patch(WATCH_TABLE, fields, {"set_number": f"eq.{n}"}, timeout=12, prefer="return=minimal")
        return r.ok
    except Exception:
        return False


def sync_set(number, full_history=True, allow_browser=True):
    n = _clean_number(number)
    if not n:
        return {"ok": False, "number": n, "error": "invalid_set_number"}
    if not trades_ready():
        return {"ok": False, "number": n, "error": "trade_table_missing"}

    ctx = product_context(n, allow_browser=allow_browser)
    if not ctx:
        _patch_watch(n, last_synced_at=_now_iso(), last_status="not_found")
        return {"ok": False, "number": n, "error": "kream_product_not_found"}

    rows, complete, used_api, used_browser = fetch_sales(ctx, full_history=full_history)
    saved = upsert_trades(rows)
    history = stored_trades(n, 5000)
    latest = history[-1] if history else None

    status = "ok" if latest else "no_trades"
    _patch_watch(
        n,
        kream_product_id=ctx.get("product_id"),
        model_number=ctx.get("model_number"),
        source_url=ctx.get("source_url"),
        last_synced_at=_now_iso(),
        last_status=status,
    )

    return {
        "ok": bool(latest),
        "number": n,
        "model_number": ctx.get("model_number"),
        "product_id": ctx.get("product_id"),
        "trade_count": len(history),
        "history_added": saved,
        "history_complete": bool(complete and used_api),
        "latest": latest,
        "used_browser": used_browser,
        "source_url": ctx.get("source_url"),
    }


def _seed_watch_from_existing_trades(max_rows=1000):
    if not watch_ready() or not trades_ready():
        return 0
    try:
        r = rest_get(
            TRADES_TABLE,
            {"select": "set_number", "order": "trade_at.desc", "limit": str(max_rows)},
            timeout=12,
        )
        rows = r.json() if r.ok else []
    except Exception:
        rows = []
    nums = []
    seen = set()
    for row in rows:
        n = _clean_number(row.get("set_number"))
        if n and n not in seen:
            seen.add(n)
            nums.append(n)
    if nums:
        watch_many(nums)
    return len(nums)


def watched_rows(limit=8):
    if not watch_ready():
        return []
    try:
        r = rest_get(
            WATCH_TABLE,
            {
                "select": "set_number,kream_product_id,model_number,source_url,last_requested_at,last_synced_at,last_status",
                "order": "last_synced_at.asc.nullsfirst,last_requested_at.desc",
                "limit": str(min(max(int(limit), 1), 50)),
            },
            timeout=12,
        )
        return r.json() if r.ok else []
    except Exception:
        return []


def sync_watched(limit=8):
    if not watch_ready():
        return {"ok": False, "error": "watch_table_missing", "requested": 0, "updated": 0}
    if not trades_ready():
        return {"ok": False, "error": "trade_table_missing", "requested": 0, "updated": 0}

    rows = watched_rows(limit)
    seeded = 0
    if not rows:
        seeded = _seed_watch_from_existing_trades()
        rows = watched_rows(limit)

    results = []
    updated = 0
    for row in rows:
        result = sync_set(row.get("set_number"), full_history=True, allow_browser=True)
        results.append(result)
        if result.get("ok"):
            updated += 1

    return {
        "ok": True,
        "requested": len(rows),
        "updated": updated,
        "seeded": seeded,
        "items": results,
    }


def chart_from_trades(trades):
    daily = {}
    for row in trades or []:
        dt = _parse_iso_utc(row.get("trade_at"))
        if not dt:
            continue
        day = dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
        price = int(row.get("price_krw") or 0)
        if price > 0:
            daily[day] = price
    all_data = [{"time": d, "value": p} for d, p in sorted(daily.items())]
    if not all_data:
        return {"1m": [], "3m": [], "6m": [], "1y": [], "all": []}
    now = datetime.now(timezone(timedelta(hours=9))).date()
    out = {"all": all_data}
    for key, days in (("1m", 31), ("3m", 93), ("6m", 186), ("1y", 366)):
        cutoff = now - timedelta(days=days)
        out[key] = [x for x in all_data if datetime.strptime(x["time"], "%Y-%m-%d").date() >= cutoff]
    return out


def history_payload(number, limit=5000):
    n = _clean_number(number)
    if not n:
        return {"ok": False, "error": "invalid_set_number", "number": n}
    trades = stored_trades(n, limit)
    recent = sorted(trades, key=lambda x: str(x.get("trade_at") or ""), reverse=True)[:100]
    for row in recent:
        row["trade_date"] = _kst_display(row.get("trade_at"))
    latest = recent[0] if recent else None
    return {
        "ok": True,
        "number": n,
        "items": recent,
        "trade_count": len(trades),
        "latest": latest,
        "charts": chart_from_trades(trades),
        "watch_table_ready": watch_ready(),
        "trade_table_ready": trades_ready(),
    }
