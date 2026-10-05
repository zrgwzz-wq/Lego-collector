import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests

from .supabase_client import configured, rest_get, rest_post, rest_patch

TRADES_TABLE = "lego_kream_trades"
WATCH_TABLE = "lego_kream_watch"
MAX_HISTORY_PAGES = 40
CHART_OPTION = "__KREAM_CHART__"
KREAM_API = "https://api.kream.co.kr"

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
        h["x-kream-api-version"] = "30"
        h["Origin"] = "https://kream.co.kr"
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
            {"set_number": f"eq.{n}", "option_name": f"neq.{CHART_OPTION}", "select": "trade_at", "order": "trade_at.desc", "limit": "1"},
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



def _visible_trade_rows(body_text, ctx):
    """Parse public completed-trade rows from current KREAM PDP layouts.

    KREAM exposes at least two public layouts:
    1) tabbed: '체결 거래 / 판매 입찰 / 구매 입찰'
    2) compact: '거래 N' followed directly by recent ONE SIZE trades

    The old parser stopped at '판매 입찰', but that label appears before the
    trade rows in the tabbed layout, which caused zero parsed transactions.
    """
    body = str(body_text or "")
    if not body:
        return []

    pos = body.find("체결 거래")
    if pos < 0:
        m = re.search(r"거래\s*[0-9,]+", body)
        pos = m.start() if m else 0

    area = body[pos:pos + 12000]

    # Only stop at markers that occur after the public trade rows.
    stops = []
    for marker in (
        "모든 시세는 로그인 후",
        "거래 내역 더보기",
        "스타일 리뷰",
        "상세 정보",
        "고객센터",
    ):
        p = area.find(marker, 1)
        if p > 0:
            stops.append(p)
    if stops:
        area = area[:min(stops)]

    rows = []
    seen = set()

    # LEGO KREAM listings use ONE SIZE. Requiring ONE SIZE + price + date keeps
    # current ask/bid prices out of the completed-trade dataset.
    pattern = re.compile(
        r"ONE\s*SIZE\s*"
        r"([1-9][0-9]{0,2}(?:,[0-9]{3})+)\s*원\s*"
        r"((?:\d{2}/\d{2}/\d{2})|(?:\d+\s*(?:분|시간|일)\s*전))",
        re.I,
    )

    for m in pattern.finditer(area):
        try:
            price = int(m.group(1).replace(",", ""))
        except Exception:
            continue
        if price < 1000 or price > 10000000:
            continue

        trade_at = _fallback_trade_time(m.group(2))
        if not trade_at:
            continue

        key = (trade_at, price)
        if key in seen:
            continue
        seen.add(key)

        rows.append({
            "set_number": ctx["number"],
            "kream_product_id": ctx["product_id"],
            "model_number": ctx.get("model_number"),
            "price_krw": price,
            "option_name": "ONE SIZE",
            "trade_at": trade_at,
            "source_url": ctx.get("source_url"),
        })

    rows.sort(key=lambda x: str(x.get("trade_at") or ""), reverse=True)
    return rows


def _chart_rows_from_payload(payload, ctx):
    """Convert KREAM's own daily chart to storage rows without treating them as transactions."""
    if not isinstance(payload, dict):
        return []
    charts = payload.get("charts") or []
    if isinstance(charts, dict):
        charts = list(charts.values())

    all_data = None
    for chart in charts:
        if isinstance(chart, dict) and str(chart.get("span") or "").lower() == "all":
            all_data = chart.get("data") or []
            break
    if all_data is None and charts:
        # Prefer the longest available series when 'all' is not explicitly present.
        candidates = [c.get("data") or [] for c in charts if isinstance(c, dict)]
        all_data = max(candidates, key=len, default=[])

    rows = []
    seen = set()
    for point in all_data or []:
        if not isinstance(point, dict):
            continue
        raw_time = point.get("time")
        dt = _parse_iso_utc(raw_time)
        if not dt:
            continue
        try:
            price = int(round(float(point.get("value") or 0)))
        except Exception:
            continue
        if price < 1000 or price > 10000000:
            continue
        trade_at = dt.isoformat().replace("+00:00", "Z")
        key = (trade_at, price)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "set_number": ctx["number"],
            "kream_product_id": ctx["product_id"],
            "model_number": ctx.get("model_number"),
            "price_krw": price,
            "option_name": CHART_OPTION,
            "trade_at": trade_at,
            "source_url": ctx.get("source_url"),
        })
    return rows



def _redact_diagnostic(value):
    text = str(value or "")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)((?:token|cookie|authorization|session|password|secret|api_key)[\w-]*[\s\"':=]+)[^\s,;&\"']+", r"\1[redacted]", text)
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[redacted]", text)
    return text[:240]


def _api_diagnostic(ctx, kind, response=None, error=None, payload=None, **metadata):
    """Log bounded status/schema/error summaries, never authentication headers."""
    entry = {"kind": kind}
    entry.update(metadata)
    if response is not None:
        entry["status"] = getattr(response, "status_code", getattr(response, "status", None))
        headers = getattr(response, "headers", {}) or {}
        entry["content_type"] = headers.get("content-type", "")[:80]
        if entry["status"] and entry["status"] >= 400:
            try:
                failure = response.json()
                if isinstance(failure, dict):
                    entry["error_keys"] = sorted(str(k) for k in failure)[:20]
                    # Only server error descriptions are logged, not arbitrary payloads.
                    for key in ("code", "error", "message", "detail"):
                        if isinstance(failure.get(key), (str, int)):
                            entry[key] = _redact_diagnostic(failure[key])
            except Exception:
                entry["error_body"] = "non_json"
    if error is not None:
        entry["error"] = _redact_diagnostic(f"{type(error).__name__}: {error}")
    if isinstance(payload, dict):
        entry["keys"] = sorted(str(k) for k in payload)[:30]
        for key in ("items", "charts"):
            value = payload.get(key)
            entry[key + "_type"] = type(value).__name__
            if isinstance(value, (list, dict)):
                entry[key + "_count"] = len(value)
    diagnostics = ctx.setdefault("diagnostics", [])
    if len(diagnostics) < 24:
        diagnostics.append(entry)


def _sales_page_requests(ctx, cursor):
    pid = ctx["product_id"]
    detail = ctx.get("source_url") or f"https://kream.co.kr/products/{pid}"
    r = requests.get(
        f"{KREAM_API}/api/p/products/{pid}/sales",
        params={"cursor": cursor, "per_page": 50, "sort": "date_created[desc]"},
        headers=_headers(detail, True),
        timeout=15,
    )
    _api_diagnostic(ctx, "sales_requests", response=r)
    if not r.ok:
        raise RuntimeError(f"http {r.status_code}")
    data = r.json() or {}
    _api_diagnostic(ctx, "sales_requests_payload", payload=data)
    if not isinstance(data, dict) or "items" not in data:
        raise ValueError("sales response missing items")
    return data


def _chart_requests(ctx):
    pid = ctx["product_id"]
    detail = ctx.get("source_url") or f"https://kream.co.kr/products/{pid}"
    r = requests.get(
        f"{KREAM_API}/api/p/products/{pid}/chart",
        headers=_headers(detail, True),
        timeout=15,
    )
    _api_diagnostic(ctx, "chart_requests", response=r)
    if not r.ok:
        raise RuntimeError(f"http {r.status_code}")
    data = r.json() or {}
    _api_diagnostic(ctx, "chart_requests_payload", payload=data)
    return data


def _native_endpoint_kind(url, product_id):
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname not in ("kream.co.kr", "api.kream.co.kr"):
        return None
    if parsed.username or parsed.password or not parsed.path.startswith("/api/"):
        return None
    match = re.search(rf"/products/{int(product_id)}/(sales|chart)/?$", parsed.path)
    return match.group(1) if match else None


def _native_request_metadata(request):
    parsed = urlparse(request.url)
    headers = request.all_headers()
    return {
        "endpoint": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
        "parameter_names": sorted({k for k, _ in parse_qsl(parsed.query)})[:30],
        "header_names": sorted(k for k in headers if k.lower().startswith("x-kream-"))[:20],
        "api_version": _redact_diagnostic(headers.get("x-kream-api-version", "")),
    }


def _sales_rows_from_payload(payload, ctx, stop_at=None):
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("sales response missing an items list")
    rows = []
    reached_old = False
    invalid = 0
    for item in payload["items"]:
        if not isinstance(item, dict):
            invalid += 1
            continue
        if str(item.get("product_id", ctx["product_id"])) != str(ctx["product_id"]):
            invalid += 1
            continue
        dt = _parse_iso_utc(item.get("date_created"))
        if not dt:
            invalid += 1
            continue
        if stop_at and dt <= stop_at:
            reached_old = True
            continue
        try:
            price = int(round(float(item.get("price") or 0)))
        except (TypeError, ValueError, OverflowError):
            price = 0
        if not 1000 <= price <= 10000000:
            invalid += 1
            continue
        option = item.get("option")
        product_option = item.get("product_option")
        if not option and isinstance(product_option, dict):
            option = product_option.get("name_display")
        rows.append({
            "set_number": ctx["number"],
            "kream_product_id": ctx["product_id"],
            "model_number": ctx.get("model_number"),
            "price_krw": price,
            "option_name": str(option or ""),
            "trade_at": dt.isoformat().replace("+00:00", "Z"),
            "source_url": ctx.get("source_url"),
        })
    return rows, reached_old, invalid


def _collect_sales_pages(ctx, first_payload, fetch_next, full_history, stop_at):
    """Only mark history complete after exhausting valid API pages."""
    rows = []
    payload = first_payload
    seen_cursors = set()
    invalid_count = 0
    received_count = 0
    for page_index in range(MAX_HISTORY_PAGES):
        try:
            page_rows, reached_old, invalid = _sales_rows_from_payload(payload, ctx, stop_at)
        except Exception as e:
            _api_diagnostic(ctx, "sales_schema", error=e)
            return rows, False
        rows.extend(page_rows)
        invalid_count += invalid
        received_count += len(payload["items"])
        next_cursor = payload.get("next_cursor")
        if not next_cursor:
            total = payload.get("total")
            # A truncated response with no usable cursor is not full history.
            try:
                truncated = total is not None and int(total) > received_count
            except (ValueError, TypeError):
                truncated = False
            complete = bool(full_history and not invalid_count and not truncated)
            if not complete:
                _api_diagnostic(ctx, "sales_partial", received=received_count,
                                invalid_rows=invalid_count, truncated=truncated)
            return rows, complete
        if reached_old or not full_history:
            return rows, False
        cursor_key = str(next_cursor)
        if cursor_key in seen_cursors or not payload["items"]:
            _api_diagnostic(ctx, "sales_pagination", error=ValueError("cursor repeated or empty page with next_cursor"))
            return rows, False
        seen_cursors.add(cursor_key)
        if page_index + 1 >= MAX_HISTORY_PAGES:
            _api_diagnostic(ctx, "sales_pagination", error=ValueError("history page limit reached"))
            return rows, False
        try:
            payload = fetch_next(next_cursor)
        except Exception as e:
            _api_diagnostic(ctx, "sales_next_page", error=e)
            return rows, False
    return rows, False


def _fetch_sales_browser(ctx, stop_at=None, full_history=True):
    """Read the requests/responses made by the actual anonymous product page.

    Native request headers/parameters stay inside this browser session. No
    hard-coded API call is substituted for an unobserved or login-gated request.
    """
    ctx["browser_attempted"] = True
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        _api_diagnostic(ctx, "browser_import", error=e)
        return [], [], False, False

    pid = ctx["product_id"]
    detail = ctx.get("source_url") or f"https://kream.co.kr/products/{pid}"
    native = {}
    observed = set()
    rows = []
    chart_rows = []
    complete = False
    browser = None

    def capture_response(response):
        kind = _native_endpoint_kind(response.url, pid)
        if not kind:
            return
        observed.add(kind)
        try:
            request = response.request
            if request.method != "GET":
                return
            _api_diagnostic(ctx, f"{kind}_page_response", response=response,
                            **_native_request_metadata(request))
            if not response.ok:
                return
            payload = response.json()
            _api_diagnostic(ctx, f"{kind}_page_payload", payload=payload)
            valid = (isinstance(payload, dict) and
                     (isinstance(payload.get("items"), list) if kind == "sales" else "charts" in payload))
            if valid:
                # Prefer the first sales page. The page can request it repeatedly.
                native.setdefault(kind, {"payload": payload, "request": request})
        except Exception as e:
            _api_diagnostic(ctx, f"{kind}_page_response", error=e)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                context = browser.new_context(
                    locale="ko-KR", user_agent=_headers()["User-Agent"],
                    viewport={"width": 1280, "height": 1600})
                page = context.new_page()
                page.on("response", capture_response)
                navigation = None
                try:
                    navigation = page.goto(detail, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    _api_diagnostic(ctx, "product_navigation", error=e)
                _api_diagnostic(ctx, "product_page", response=navigation)
                # Some responses have an error HTTP status while their rendered
                # product body remains usable. Read the DOM before deciding.
                page.wait_for_timeout(1800)
                body = page.locator("body").inner_text(timeout=8000)
                rows = _visible_trade_rows(body, ctx)
                model_number = str(ctx.get("model_number") or ctx["number"])
                product_visible = bool(re.search(rf"(?<!\d){re.escape(model_number)}(?!\d)", body))
                error_page = bool(re.search(r"\b(?:500|502|503)\b|internal server error|bad gateway|peer closed connection", body, re.I))
                body_state = ("product_rendered" if product_visible or rows or native else
                              "server_error" if error_page else "other_page" if body.strip() else "blank")
                # null means the page could not establish whether login is required.
                login_required = (bool(re.search(r"(?:모든\s*)?시세.*로그인\s*후|로그인.*시세.*확인", body))
                                  if body_state == "product_rendered" else None)
                ctx["login_required"] = login_required
                if _bad_page_text(body):
                    _api_diagnostic(ctx, "product_page_blocked", reason="page_access_restricted")
                    return rows, [], False, bool(rows)

                # A visible trade tab may initiate a request when not gated.
                # Never click through a login prompt or attempt authentication.
                if "sales" not in native and login_required is False:
                    try:
                        tab = page.get_by_text("체결 거래", exact=True)
                        if tab.count() == 1 and tab.is_visible():
                            tab.click(timeout=3000)
                            page.wait_for_timeout(1200)
                    except Exception as e:
                        _api_diagnostic(ctx, "trade_tab", error=e)

                _api_diagnostic(ctx, "page_observation", login_required=login_required,
                                body_state=body_state, body_length=len(body),
                                product_number_visible=product_visible,
                                native_sales_observed="sales" in observed,
                                native_chart_observed="chart" in observed,
                                native_sales_ok="sales" in native,
                                native_chart_ok="chart" in native,
                                visible_trade_count=len(rows))
                if "chart" in native:
                    chart_rows = _chart_rows_from_payload(native["chart"]["payload"], ctx)
                if "sales" in native:
                    sample = native["sales"]
                    request = sample["request"]
                    parsed = urlparse(request.url)
                    first_parameters = dict(parse_qsl(parsed.query, keep_blank_values=True))
                    first_cursor = first_parameters.get("cursor", "1")
                    if first_cursor not in ("", "1"):
                        _api_diagnostic(ctx, "sales_partial", reason="observed page is not first cursor")
                        api_rows, _, _ = _sales_rows_from_payload(sample["payload"], ctx, stop_at)
                    else:
                        headers = {k: v for k, v in request.all_headers().items()
                                   if not k.startswith(":") and k.lower() not in
                                   ("host", "content-length", "connection", "cookie")}

                        def fetch_next(cursor):
                            pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                                     if k != "cursor"]
                            pairs.append(("cursor", str(cursor)))
                            url = urlunparse(parsed._replace(query=urlencode(pairs), fragment=""))
                            response = context.request.get(url, headers=headers, timeout=15000)
                            _api_diagnostic(ctx, "sales_browser_next", response=response)
                            if not response.ok:
                                raise RuntimeError(f"http {response.status}")
                            return response.json()

                        api_rows, complete = _collect_sales_pages(
                            ctx, sample["payload"], fetch_next, full_history, stop_at)
                    ctx["browser_sales_api_ok"] = True
                    if api_rows:
                        rows = api_rows
            finally:
                if browser is not None:
                    browser.close()
    except Exception as e:
        _api_diagnostic(ctx, "browser_session", error=e)
        complete = False

    rows.sort(key=lambda x: str(x.get("trade_at") or ""), reverse=True)
    chart_rows.sort(key=lambda x: str(x.get("trade_at") or ""))
    return rows, chart_rows, complete, bool(rows or chart_rows or ctx.get("browser_sales_api_ok"))


def fetch_sales(ctx, full_history=True, allow_browser=True):
    ctx["diagnostics"] = []
    ctx["browser_sales_api_ok"] = False
    ctx["browser_attempted"] = False
    ctx["login_required"] = None
    stop_at = None if full_history else _parse_iso_utc(_latest_stored_trade_at(ctx["number"]))
    rows = []
    chart_rows = []
    complete = False
    used_api = False
    used_browser = False

    # Keep the existing fast API route, but native page responses are the fallback.
    try:
        chart_rows = _chart_rows_from_payload(_chart_requests(ctx), ctx)
    except Exception as e:
        _api_diagnostic(ctx, "chart_requests", error=e)
    try:
        first_payload = _sales_page_requests(ctx, 1)
        used_api = True
        rows, complete = _collect_sales_pages(
            ctx, first_payload, lambda cursor: _sales_page_requests(ctx, cursor), full_history, stop_at)
    except Exception as e:
        _api_diagnostic(ctx, "sales_requests", error=e)

    if allow_browser and (not used_api or not complete or not chart_rows):
        browser_rows, browser_chart, browser_complete, browser_ok = _fetch_sales_browser(
            ctx, stop_at=stop_at, full_history=full_history)
        used_browser = browser_ok
        if browser_chart:
            chart_rows = browser_chart
        if ctx.get("browser_sales_api_ok") and not complete and (browser_complete or len(browser_rows) >= len(rows)):
            rows = browser_rows
            complete = browser_complete
            used_api = True
        elif not rows:
            rows = browser_rows
    if not rows and not used_api:
        rows = _public_html_trades(ctx)
    rows.sort(key=lambda x: str(x.get("trade_at") or ""), reverse=True)
    return rows, chart_rows, complete, used_api, used_browser


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
                "option_name": f"neq.{CHART_OPTION}",
                "select": "set_number,kream_product_id,model_number,price_krw,option_name,trade_at,source_url",
                "order": "trade_at.asc",
                "limit": str(min(max(int(limit), 1), 10000)),
            },
            timeout=15,
        )
        return r.json() if r.ok else []
    except Exception:
        return []


def stored_chart_rows(number, limit=10000):
    if not trades_ready():
        return []
    n = _clean_number(number)
    try:
        r = rest_get(
            TRADES_TABLE,
            {
                "set_number": f"eq.{n}",
                "option_name": f"eq.{CHART_OPTION}",
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

    rows, chart_rows, complete, used_api, used_browser = fetch_sales(
        ctx, full_history=full_history, allow_browser=allow_browser)
    saved = upsert_trades(rows)
    chart_saved = upsert_trades(chart_rows)
    history = stored_trades(n, 5000)
    chart_history = stored_chart_rows(n, 10000)
    latest = history[-1] if history else None

    refresh_succeeded = bool(rows or chart_rows or (used_api and complete))
    storage_succeeded = bool(refresh_succeeded and saved == len(rows) and chart_saved == len(chart_rows))
    ok = refresh_succeeded and storage_succeeded
    status = ("ok" if ok and complete else "partial_history" if ok else
              "store_failed" if refresh_succeeded else
              "refresh_failed_cached" if latest else "refresh_failed")
    _patch_watch(
        n,
        kream_product_id=ctx.get("product_id"),
        model_number=ctx.get("model_number"),
        source_url=ctx.get("source_url"),
        last_synced_at=_now_iso(),
        last_status=status,
    )

    return {
        "ok": ok,
        "refresh_succeeded": refresh_succeeded,
        "storage_succeeded": storage_succeeded,
        "has_stored_trades": bool(latest),
        "fetched_trade_count": len(rows),
        "fetched_chart_count": len(chart_rows),
        "status": status,
        "number": n,
        "model_number": ctx.get("model_number"),
        "product_id": ctx.get("product_id"),
        "trade_count": len(history),
        "history_added": saved,
        "chart_added": chart_saved,
        "chart_count": len(chart_history),
        "history_complete": bool(complete and used_api),
        "latest": latest,
        "used_browser": used_browser,
        "browser_attempted": ctx.get("browser_attempted", False),
        "used_api": used_api,
        "login_required": ctx.get("login_required"),
        "diagnostics": ctx.get("diagnostics", []),
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
    successful = 0
    for row in rows:
        result = sync_set(row.get("set_number"), full_history=True, allow_browser=True)
        results.append(result)
        if result.get("ok"):
            successful += 1
            if result.get("history_added", 0) or result.get("chart_added", 0):
                updated += 1

    return {
        "ok": all(result.get("ok") for result in results),
        "requested": len(rows),
        "updated": updated,
        "successful": successful,
        "failed": len(results) - successful,
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
    chart_rows = stored_chart_rows(n, 10000)
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
        "charts": chart_from_trades(chart_rows if chart_rows else trades),
        "chart_point_count": len(chart_rows),
        "watch_table_ready": watch_ready(),
        "trade_table_ready": trades_ready(),
    }
