
import os
import requests

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

def configured():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

def auth_configured():
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)

def service_headers(prefer=None):
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h

def user_headers(access_token, prefer=None):
    h = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h

def rest_get(table, params=None, timeout=10, headers=None):
    h = headers or service_headers()
    return requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=h, params=params or {}, timeout=timeout)

def rest_post(table, payload, params=None, timeout=15, headers=None, prefer=None):
    h = headers or service_headers(prefer)
    return requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=h, params=params or {}, json=payload, timeout=timeout)

def rest_patch(table, payload, params=None, timeout=15, headers=None, prefer=None):
    h = headers or service_headers(prefer)
    return requests.patch(f"{SUPABASE_URL}/rest/v1/{table}", headers=h, params=params or {}, json=payload, timeout=timeout)

def rest_delete(table, params=None, timeout=15, headers=None, prefer=None):
    h = headers or service_headers(prefer)
    return requests.delete(f"{SUPABASE_URL}/rest/v1/{table}", headers=h, params=params or {}, timeout=timeout)

def rpc(name, payload, timeout=15, headers=None):
    h = headers or service_headers()
    return requests.post(f"{SUPABASE_URL}/rest/v1/rpc/{name}", headers=h, json=payload, timeout=timeout)

def auth_post(path, payload, timeout=15):
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
    }
    return requests.post(f"{SUPABASE_URL}/auth/v1/{path.lstrip('/')}", headers=headers, json=payload, timeout=timeout)

def auth_get_user(access_token, timeout=10):
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {access_token}",
    }
    return requests.get(f"{SUPABASE_URL}/auth/v1/user", headers=headers, timeout=timeout)
