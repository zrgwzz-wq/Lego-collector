
import os
import time
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory

from services import catalog, kream
from services.supabase_client import (
    SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY,
    configured as sb_configured, auth_configured,
    auth_post, auth_get_user, rest_get, rest_post, rest_patch, rest_delete,
    user_headers
)

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=None)

def bearer_token():
    raw = request.headers.get("Authorization", "")
    return raw[7:].strip() if raw.lower().startswith("bearer ") else ""

def require_user_token():
    token = bearer_token()
    if not token:
        return None, (jsonify(ok=False, error="login_required"), 401)
    return token, None

def json_or_error(r, fallback="request_failed"):
    try:
        data = r.json() if r.text else {}
    except Exception:
        data = {"message": r.text[:300]}
    if r.ok:
        return data, None
    return None, (jsonify(ok=False, error=fallback, detail=data), r.status_code)

@app.get("/")
def home():
    return send_from_directory(ROOT, "index.html")

@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(ROOT, "manifest.webmanifest", mimetype="application/manifest+json")

@app.get("/sw.js")
def sw():
    r = send_from_directory(ROOT, "sw.js", mimetype="application/javascript")
    r.headers["Cache-Control"] = "no-cache"
    return r

@app.get("/api/v2/health")
def health():
    st = catalog.sync_status() if sb_configured() else {"count": 0, "table_ready": False}
    return jsonify(
        ok=True,
        version="2.2",
        supabase_configured=sb_configured(),
        auth_configured=auth_configured(),
        brickset_configured=bool(os.getenv("BRICKSET_API_KEY")),
        catalog_count=st.get("count", 0),
        catalog_ready=st.get("table_ready", False),
        catalog_initial_complete=st.get("initial_complete", False),
        last_sync_date=st.get("last_sync_date"),
    )

# ---------- Catalog: DB-only user path ----------
@app.get("/api/v2/catalog")
def catalog_list():
    d = catalog.search_catalog(
        q=request.args.get("q", ""),
        theme=request.args.get("theme", ""),
        subtheme=request.args.get("subtheme", ""),
        year=request.args.get("year"),
        category=request.args.get("category", ""),
        sort=request.args.get("sort", "newest"),
        page=request.args.get("page", 1),
        page_size=request.args.get("page_size", 40),
        released=request.args.get("released", "true").lower() != "false",
    )
    return jsonify(d)

@app.get("/api/v2/catalog/<set_number>")
def catalog_detail(set_number):
    row = catalog.get_set(set_number)
    if not row:
        return jsonify(ok=False, error="not_found"), 404
    return jsonify(ok=True, item=row)

@app.get("/api/v2/catalog-facets")
def catalog_facets():
    return jsonify(ok=True, **catalog.facets())

@app.get("/api/v2/catalog-sync-status")
def catalog_sync_status():
    return jsonify(catalog.sync_status())

# ---------- Shared market data: no user identity is stored ----------
@app.post("/api/v2/kream-watch")
def kream_watch():
    body = request.get_json(silent=True) or {}
    raw = body.get("numbers") or []
    if not isinstance(raw, list):
        raw = [raw]
    return jsonify(kream.watch_many(raw[:100]))

@app.get("/api/v2/kream-history/<set_number>")
def kream_history(set_number):
    return jsonify(kream.history_payload(set_number, 5000))

# ---------- Supabase Auth proxy ----------
@app.post("/api/v2/auth/signup")
def signup():
    if not auth_configured():
        return jsonify(ok=False, error="auth_not_configured"), 503
    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "")
    if not email or len(password) < 6:
        return jsonify(ok=False, error="invalid_credentials"), 400
    r = auth_post("signup", {"email": email, "password": password})
    data, err = json_or_error(r, "signup_failed")
    if err: return err
    return jsonify(ok=True, data=data)

@app.post("/api/v2/auth/login")
def login():
    if not auth_configured():
        return jsonify(ok=False, error="auth_not_configured"), 503
    body = request.get_json(silent=True) or {}
    r = auth_post("token?grant_type=password", {
        "email": str(body.get("email") or "").strip(),
        "password": str(body.get("password") or ""),
    })
    data, err = json_or_error(r, "login_failed")
    if err: return err
    return jsonify(ok=True, data=data)

@app.post("/api/v2/auth/refresh")
def refresh():
    if not auth_configured():
        return jsonify(ok=False, error="auth_not_configured"), 503
    body = request.get_json(silent=True) or {}
    r = auth_post("token?grant_type=refresh_token", {"refresh_token": body.get("refresh_token")})
    data, err = json_or_error(r, "refresh_failed")
    if err: return err
    return jsonify(ok=True, data=data)

@app.get("/api/v2/auth/me")
def me():
    token, err = require_user_token()
    if err: return err
    r = auth_get_user(token)
    data, e = json_or_error(r, "invalid_session")
    if e: return e
    return jsonify(ok=True, user=data)

# ---------- Cloud collection: RLS-enforced with user's JWT ----------
COL_SELECT = (
    "id,set_number,condition,purchase_price,purchase_date,current_value,valuation_source,"
    "sold,sold_price,sold_date,memo,created_at,updated_at,"
    "lego_master_catalog(set_number,name_ko,name_en,name_ko_source,theme,subtheme,category,year,pieces,"
    "minifigs,price_krw,launch_date,thumbnail_url,image_url)"
)

def user_rest_headers(token, prefer=None):
    if not auth_configured():
        return None
    return user_headers(token, prefer)

@app.get("/api/v2/collection")
def collection_get():
    token, err = require_user_token()
    if err: return err
    h = user_rest_headers(token)
    try:
        r = rest_get("user_collection_items", {
            "select": COL_SELECT,
            "order": "created_at.desc"
        }, timeout=15, headers=h)
    except Exception as e:
        return jsonify(ok=False, error=type(e).__name__), 500
    data, e = json_or_error(r, "collection_load_failed")
    if e: return e
    return jsonify(ok=True, items=data or [])

@app.post("/api/v2/collection")
def collection_add():
    token, err = require_user_token()
    if err: return err
    body = request.get_json(silent=True) or {}
    set_number = str(body.get("set_number") or "").strip().split("-")[0]
    if not set_number:
        return jsonify(ok=False, error="set_number_required"), 400
    row = {
        "set_number": set_number,
        "condition": body.get("condition") or "미개봉",
        "purchase_price": body.get("purchase_price"),
        "purchase_date": body.get("purchase_date") or None,
        "current_value": body.get("current_value"),
        "valuation_source": body.get("valuation_source") or "정가 기준",
        "memo": body.get("memo") or "",
    }
    # user_id is filled by DB trigger using auth.uid(); client cannot impersonate another user.
    h = user_rest_headers(token, "return=representation")
    r = rest_post("user_collection_items", row, timeout=15, headers=h)
    data, e = json_or_error(r, "collection_add_failed")
    if e: return e
    return jsonify(ok=True, item=(data or [None])[0] if isinstance(data, list) else data)

@app.patch("/api/v2/collection/<item_id>")
def collection_update(item_id):
    token, err = require_user_token()
    if err: return err
    body = request.get_json(silent=True) or {}
    allowed = {
        "condition", "purchase_price", "purchase_date", "current_value", "valuation_source",
        "sold", "sold_price", "sold_date", "memo"
    }
    row = {k: body[k] for k in allowed if k in body}
    if not row:
        return jsonify(ok=True)
    row["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    h = user_rest_headers(token, "return=representation")
    r = rest_patch("user_collection_items", row, {"id": f"eq.{item_id}"}, timeout=15, headers=h)
    data, e = json_or_error(r, "collection_update_failed")
    if e: return e
    return jsonify(ok=True, item=(data or [None])[0] if isinstance(data, list) else data)

@app.delete("/api/v2/collection/<item_id>")
def collection_delete(item_id):
    token, err = require_user_token()
    if err: return err
    h = user_rest_headers(token, "return=minimal")
    r = rest_delete("user_collection_items", {"id": f"eq.{item_id}"}, timeout=15, headers=h)
    _, e = json_or_error(r, "collection_delete_failed")
    if e: return e
    return jsonify(ok=True)

@app.post("/api/v2/collection/import")
def collection_import():
    token, err = require_user_token()
    if err: return err
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        return jsonify(ok=False, error="items_required"), 400
    clean = []
    for x in items[:1000]:
        n = str((x or {}).get("set_number") or "").strip().split("-")[0]
        if not n: continue
        clean.append({
            "set_number": n,
            "condition": (x or {}).get("condition") or "미개봉",
            "purchase_price": (x or {}).get("purchase_price"),
            "purchase_date": (x or {}).get("purchase_date") or None,
            "current_value": (x or {}).get("current_value"),
            "valuation_source": (x or {}).get("valuation_source") or "기존 앱 가져오기",
            "sold": bool((x or {}).get("sold")),
            "sold_price": (x or {}).get("sold_price"),
            "sold_date": (x or {}).get("sold_date") or None,
            "memo": (x or {}).get("memo") or "",
        })
    if not clean:
        return jsonify(ok=False, error="no_valid_items"), 400
    h = user_rest_headers(token, "return=minimal")
    imported = 0
    failed = 0
    # 한 세트가 마스터 DB에 아직 없더라도 나머지 항목은 계속 가져오도록 개별 처리
    for row in clean:
        try:
            r = rest_post("user_collection_items", row, timeout=12, headers=h)
            if r.ok:
                imported += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return jsonify(ok=imported > 0, imported=imported, failed=failed)

# ---------- Wishlist ----------
WISH_SELECT = (
    "id,set_number,target_price,memo,created_at,updated_at,"
    "lego_master_catalog(set_number,name_ko,name_en,theme,year,pieces,price_krw,launch_date,thumbnail_url,image_url)"
)

@app.get("/api/v2/wishlist")
def wishlist_get():
    token, err = require_user_token()
    if err: return err
    h = user_rest_headers(token)
    r = rest_get("user_wishlist", {"select": WISH_SELECT, "order": "created_at.desc"}, timeout=15, headers=h)
    data, e = json_or_error(r, "wishlist_load_failed")
    if e: return e
    return jsonify(ok=True, items=data or [])

@app.post("/api/v2/wishlist")
def wishlist_add():
    token, err = require_user_token()
    if err: return err
    body = request.get_json(silent=True) or {}
    n = str(body.get("set_number") or "").strip().split("-")[0]
    if not n:
        return jsonify(ok=False, error="set_number_required"), 400
    row = {"set_number": n, "target_price": body.get("target_price"), "memo": body.get("memo") or ""}
    h = user_rest_headers(token, "resolution=merge-duplicates,return=representation")
    r = rest_post("user_wishlist", row, {"on_conflict": "user_id,set_number"}, timeout=15, headers=h)
    data, e = json_or_error(r, "wishlist_add_failed")
    if e: return e
    return jsonify(ok=True, item=(data or [None])[0] if isinstance(data, list) else data)

@app.delete("/api/v2/wishlist/<item_id>")
def wishlist_delete(item_id):
    token, err = require_user_token()
    if err: return err
    h = user_rest_headers(token, "return=minimal")
    r = rest_delete("user_wishlist", {"id": f"eq.{item_id}"}, timeout=15, headers=h)
    _, e = json_or_error(r, "wishlist_delete_failed")
    if e: return e
    return jsonify(ok=True)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
