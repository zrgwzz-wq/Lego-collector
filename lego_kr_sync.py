import argparse
import json
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from services.catalog import (
    MASTER_TABLE, state_get, state_set,
    table_ready, upsert_aliases, _now_iso, _today
)
from services.supabase_client import configured, rest_get, rest_post

LEGO_KR = "https://www.lego.com"
SHOP_URL = "https://www.lego.com/ko-kr/categories/all-sets"
KR_CATALOG_TABLE = "lego_kr_catalog"

PROVENANCE_TABLE = "lego_kr_provenance"

# 검증된 국내 발매가 fallback.
# 현재 LEGO Korea 공식몰에서 단종되어 가격이 사라진 제품만 이 목록에 둡니다.
# 화면 표시용 한글명과는 분리하여 price_krw만 보강합니다.
VERIFIED_RELEASE_PRICE_FALLBACKS = {
    "76218": {
        "price_krw": 329900,
        "price_source": "KREAM 발매가 (검증)",
        "price_type": "release_price",
        "source_url": "https://kream.co.kr/products/72845",
    },
}


def _apply_verified_release_price_fallbacks():
    if not configured() or not table_ready():
        return {"ok": False, "applied": 0, "skipped": 0}

    applied = 0
    skipped = 0
    ok = True
    now = _now_iso()

    for set_number, info in VERIFIED_RELEASE_PRICE_FALLBACKS.items():
        try:
            existing = rest_get(
                MASTER_TABLE,
                {"select": "set_number,price_krw", "set_number": f"eq.{set_number}", "limit": "1"},
                timeout=10,
            )
            rows = existing.json() if existing.ok else []
            if not rows:
                skipped += 1
                continue

            # LEGO Korea에서 이미 공식 가격을 확보했다면 fallback으로 덮어쓰지 않습니다.
            if rows[0].get("price_krw") is not None:
                skipped += 1
                continue

            rr = rest_post(
                MASTER_TABLE,
                {
                    "set_number": set_number,
                    "price_krw": info["price_krw"],
                    "updated_at": now,
                },
                {"on_conflict": "set_number"},
                timeout=15,
                prefer="resolution=merge-duplicates,return=minimal",
            )
            ok = ok and rr.ok
            if rr.ok:
                applied += 1

            # 가격 출처는 이름 출처와 분리해서 provenance 테이블에 기록합니다.
            try:
                rest_post(
                    PROVENANCE_TABLE,
                    {
                        "set_number": set_number,
                        "price_source": info["price_source"],
                        "price_type": info["price_type"],
                        "updated_at": now,
                    },
                    {"on_conflict": "set_number"},
                    timeout=12,
                    prefer="resolution=merge-duplicates,return=minimal",
                )
            except Exception:
                pass

        except Exception:
            ok = False

    return {"ok": ok, "applied": applied, "skipped": skipped}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0 Mobile Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
}

PRICE_RE = re.compile(r'([0-9]{1,3}(?:,[0-9]{3})+)\s*원')
SET_RE = re.compile(r'-(\d{4,8})(?:[/?#]|$)')
BAD_NAMES = {
    "출시 예정", "장바구니 담기", "백오더", "품절", "신제품",
    "쇼핑하기", "자세히 보기", "제품 보기", "세트 보기"
}


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _clean_name(s):
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    s = re.sub(r'\s*[0-9]{1,3}(?:,[0-9]{3})+\s*원.*$', '', s).strip()
    return s


def _good_name(name):
    name = _clean_name(name)
    if not name or name in BAD_NAMES or len(name) > 180:
        return None
    if PRICE_RE.fullmatch(name):
        return None
    return name


def _product_from_anchor(a):
    href = a.get("href") or ""
    if "/product/" not in href:
        return None
    m = SET_RE.search(href)
    if not m:
        return None
    set_number = m.group(1)

    name = _good_name(" ".join(a.stripped_strings))
    if not name:
        for tag in ("h1", "h2", "h3", "h4"):
            h = a.find(tag)
            if h:
                name = _good_name(" ".join(h.stripped_strings))
                if name:
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

    if not name:
        return None

    return {
        "set_number": set_number,
        "name_ko": name,
        "price_krw": price,
        "source_url": urljoin(LEGO_KR, href),
    }


def _persist_korean_rows(items, source):
    now = _now_iso()
    today = _today()
    master_rows = []
    kr_rows = []

    for item in items:
        n = str(item.get("set_number") or "")
        name = _good_name(item.get("name_ko"))
        if not n or not name:
            continue
        master_rows.append({
            "set_number": n,
            "name_ko": name,
            "name_ko_source": source,
            "name_ko_updated_at": now,
            "price_krw": item.get("price_krw"),
            "updated_at": now,
        })
        kr_rows.append({
            "set_number": n,
            "name_ko": name,
            "price_krw": item.get("price_krw"),
            "source": source,
            "source_url": item.get("source_url"),
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

    return ok, len(master_rows), sum(1 for x in master_rows if x.get("price_krw") is not None)


def _sync_shop_requests(max_pages):
    s = _session()
    seen = {}
    pages_done = 0
    empty_pages = 0

    for page_no in range(1, max_pages + 1):
        url = SHOP_URL if page_no == 1 else f"{SHOP_URL}?page={page_no}"
        try:
            r = s.get(url, timeout=30)
            if r.status_code >= 400:
                return None, {
                    "error": "http_error",
                    "status": r.status_code,
                    "page": page_no,
                }
        except Exception as e:
            return None, {
                "error": type(e).__name__,
                "page": page_no,
            }

        soup = BeautifulSoup(r.text, "html.parser")
        before = len(seen)
        for a in soup.find_all("a", href=True):
            item = _product_from_anchor(a)
            if not item:
                continue
            n = item["set_number"]
            prev = seen.get(n)
            if prev is None or (prev.get("price_krw") is None and item.get("price_krw") is not None):
                seen[n] = item

        pages_done += 1
        empty_pages = empty_pages + 1 if len(seen) == before else 0
        if empty_pages >= 2:
            break
        time.sleep(0.20)

    return list(seen.values()), {"pages": pages_done, "method": "requests"}


def _sync_shop_browser(max_pages):
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return None, {"error": f"playwright_import:{type(e).__name__}"}

    seen = {}
    pages_done = 0
    empty_pages = 0
    last_status = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            locale="ko-KR",
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        for page_no in range(1, max_pages + 1):
            url = SHOP_URL if page_no == 1 else f"{SHOP_URL}?page={page_no}"
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                last_status = response.status if response else None
                if last_status and last_status >= 400:
                    browser.close()
                    return None, {
                        "error": "browser_http_error",
                        "status": last_status,
                        "page": page_no,
                    }
                page.wait_for_timeout(1200)
            except Exception as e:
                browser.close()
                return None, {
                    "error": f"browser_navigation:{type(e).__name__}",
                    "page": page_no,
                    "status": last_status,
                }

            raw = page.evaluate(r'''() => {
              const out = [];
              const anchors = [...document.querySelectorAll('a[href*="/product/"]')];
              const priceRe = /([0-9]{1,3}(?:,[0-9]{3})+)\s*원/;

              for (const a of anchors) {
                const href = a.href || a.getAttribute('href') || '';
                const m = href.match(/-(\d{4,8})(?:[/?#]|$)/);
                if (!m) continue;

                // LEGO's card markup changes often. Do not trust one "product"
                // class: walk upward and use the smallest ancestor that actually
                // contains a KRW price.
                let priceText = '';
                let contextText = '';
                let heading = '';
                let node = a;

                for (let i = 0; node && i < 12; i++, node = node.parentElement) {
                  const txt = (node.innerText || '').trim();
                  if (!heading) {
                    heading = node.querySelector?.('h1,h2,h3,h4')?.innerText || '';
                  }
                  if (!contextText && txt.length > 0 && txt.length < 2500) {
                    contextText = txt;
                  }
                  if (txt.length > 0 && txt.length < 12000 && priceRe.test(txt)) {
                    priceText = txt;
                    contextText = txt;
                    break;
                  }
                }

                // Also look at a few nearby siblings if price is rendered next to
                // rather than inside the link container.
                if (!priceText) {
                  let parent = a.parentElement;
                  for (let depth = 0; parent && depth < 6 && !priceText; depth++, parent = parent.parentElement) {
                    const siblings = [...(parent.children || [])];
                    for (const sib of siblings) {
                      const txt = (sib.innerText || '').trim();
                      if (txt.length > 0 && txt.length < 8000 && priceRe.test(txt)) {
                        priceText = txt;
                        contextText = txt;
                        break;
                      }
                    }
                  }
                }

                const aria = a.getAttribute('aria-label') || '';
                const imgAlt = a.querySelector('img')?.getAttribute('alt') || '';
                const anchorText = a.innerText || '';

                out.push({
                  href,
                  set_number: m[1],
                  heading,
                  aria,
                  imgAlt,
                  anchorText,
                  cardText: priceText || contextText
                });
              }
              return out;
            }''')

            before = len(seen)
            for row in raw or []:
                candidates = [row.get("heading"), row.get("aria"), row.get("anchorText"), row.get("imgAlt")]
                name = None
                for candidate in candidates:
                    candidate = _good_name(candidate)
                    if candidate:
                        name = candidate
                        break
                if not name:
                    continue
                price = None
                pm = PRICE_RE.search(str(row.get("cardText") or ""))
                if pm:
                    try:
                        price = int(pm.group(1).replace(",", ""))
                    except Exception:
                        pass
                item = {
                    "set_number": row["set_number"],
                    "name_ko": name,
                    "price_krw": price,
                    "source_url": row.get("href"),
                }
                prev = seen.get(item["set_number"])
                if prev is None or (prev.get("price_krw") is None and price is not None):
                    seen[item["set_number"]] = item

            pages_done += 1
            empty_pages = empty_pages + 1 if len(seen) == before else 0
            if empty_pages >= 2:
                break

        browser.close()

    return list(seen.values()), {
        "pages": pages_done,
        "method": "playwright",
        "status": last_status,
    }


def sync_current_shop(max_pages=55, force=False):
    if not configured() or not table_ready():
        return {"ok": False, "error": "supabase_not_ready"}

    verified_release_prices = _apply_verified_release_price_fallbacks()

    last_day = state_get("lego_kr_shop_last_sync", None)
    try:
        last_price_count = int(state_get("lego_kr_shop_last_price_count", 0) or 0)
    except Exception:
        last_price_count = 0

    # A prior run that saved names but captured zero prices is considered incomplete
    # and will be retried the same day.
    if not force and last_day == _today() and last_price_count > 0:
        return {
            "ok": True,
            "skipped": True,
            "reason": "already_synced_today",
            "prices_found": last_price_count,
            "verified_release_prices": verified_release_prices,
        }

    items, meta = _sync_shop_requests(max_pages)
    fallback = None
    if items is None or len(items) < 50:
        fallback = meta
        items, meta = _sync_shop_browser(max_pages)

    if items is None:
        return {
            "ok": False,
            "error": meta.get("error") if meta else "shop_fetch_failed",
            "detail": meta,
            "requests_attempt": fallback,
            "products_found": 0,
        }

    ok, saved, priced = _persist_korean_rows(items, "LEGO Korea 공식몰")
    if ok and saved:
        state_set("lego_kr_shop_last_sync", _today())
        state_set("lego_kr_shop_last_price_count", priced)

    return {
        "ok": ok,
        "pages": meta.get("pages"),
        "method": meta.get("method"),
        "products_found": saved,
        "prices_found": priced,
        "requests_attempt": fallback,
        "verified_release_prices": verified_release_prices,
        "source": "LEGO Korea 공식몰",
    }


def _instruction_name_requests(s, set_number):
    url = f"https://www.lego.com/ko-kr/service/building-instructions/{set_number}"
    try:
        r = s.get(url, timeout=20)
        if r.status_code != 200:
            return None
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for h in soup.find_all("h1"):
        name = _good_name(" ".join(h.stripped_strings))
        if not name:
            continue
        low = name.lower()
        if "조립 설명서" in name or "building instruction" in low:
            continue
        return {"name_ko": name, "source_url": url}
    return None


def _instruction_names_browser(set_numbers):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {}, "playwright_import_failed"

    found = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            locale="ko-KR",
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        for n in set_numbers:
            url = f"https://www.lego.com/ko-kr/service/building-instructions/{n}"
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=25000)
                if response and response.status >= 400:
                    continue
                page.wait_for_timeout(450)
                heads = page.locator("h1").all_inner_texts()
                name = None
                for h in heads:
                    h = _good_name(h)
                    if not h:
                        continue
                    low = h.lower()
                    if "조립 설명서" in h or "building instruction" in low:
                        continue
                    name = h
                    break
                if name:
                    found[n] = {"name_ko": name, "source_url": page.url}
            except Exception:
                continue

        browser.close()
    return found, "playwright"


def sync_instruction_names(limit=80):
    """Gradually add official Korean names for retired/older sets.
    No machine translation is used. Requests is tried first; Playwright fills
    names that are rendered client-side.
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

    set_numbers = [str(x.get("set_number") or "") for x in rows if x.get("set_number")]
    last = set_numbers[-1] if set_numbers else cursor
    s = _session()
    found_map = {}

    # Cheap first pass.
    for n in set_numbers:
        item = _instruction_name_requests(s, n)
        if item:
            found_map[n] = item

    missing = [n for n in set_numbers if n not in found_map]
    browser_found = {}
    browser_method = None
    if missing:
        browser_found, browser_method = _instruction_names_browser(missing)
        found_map.update(browser_found)

    items = [{
        "set_number": n,
        "name_ko": item["name_ko"],
        "price_krw": None,
        "source_url": item["source_url"],
    } for n, item in found_map.items()]

    ok, saved, _ = _persist_korean_rows(items, "LEGO Korea 조립 설명서")
    state_set("lego_kr_name_cursor", last)

    return {
        "ok": ok,
        "checked": len(set_numbers),
        "found": saved,
        "requests_found": saved - len(browser_found),
        "browser_found": len(browser_found),
        "browser_method": browser_method,
        "cursor": last,
        "source": "LEGO Korea 조립 설명서",
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--shop-pages", type=int, default=55)
    p.add_argument("--instruction-names", type=int, default=80)
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
