import os, time,json,time,requests,re
from flask import Flask,request,jsonify,send_from_directory
app=Flask(__name__); KEY=os.environ.get("BRICKSET_API_KEY","")
API="https://brickset.com/api/v3.asmx"
CACHE={}; TTL=21600
@app.get("/")
def home(): return send_from_directory(".","index.html")
@app.get("/api/health")
def health():
    return jsonify(
        ok=True,
        key_configured=bool(KEY),
        cache_items=len(CACHE),
        kr_catalog_items=len(load_kr_catalog()) if "load_kr_catalog" in globals() else 0,
        supabase_configured=bool(os.environ.get("SUPABASE_URL","") and os.environ.get("SUPABASE_SERVICE_KEY","")),
        version="v24"
    )
@app.get("/api/search")
def search():
    if not KEY:return jsonify(error="BRICKSET_API_KEY 미설정"),500
    q=request.args.get("q","").strip(); ck=q.lower()
    if ck in CACHE and time.time()-CACHE[ck]["t"]<TTL:
        out=dict(CACHE[ck]["d"]); out["cached"]=True; return jsonify(out)
    p={"apiKey":KEY,"userHash":"","params":json.dumps({"query":q,"pageSize":20,"extendedData":1})}
    r=requests.get(API+"/getSets",params=p,timeout=20);r.raise_for_status()
    d=r.json(); CACHE[ck]={"t":time.time(),"d":d}; d["cached"]=False; return jsonify(d)
@app.get("/api/usage")
def usage():
    if not KEY:return jsonify(error="BRICKSET_API_KEY 미설정"),500
    r=requests.get(API+"/getKeyUsageStats",params={"apiKey":KEY},timeout=20);r.raise_for_status()
    return jsonify(r.json())



KR_CATALOG_PATH=os.path.join(os.path.dirname(__file__),"kr_catalog.json")
def load_kr_catalog():
    try:
        with open(KR_CATALOG_PATH,"r",encoding="utf-8") as f: return json.load(f)
    except: return {}

@app.get("/api/kr-meta")
def kr_meta():
    number=re.sub(r"[^0-9]","",request.args.get("number",""))
    data=load_kr_catalog().get(number)
    if not data: return jsonify(found=False,number=number)
    return jsonify(found=True,number=number,**data)

@app.get("/api/kr-catalog")
def kr_catalog():
    data=load_kr_catalog()
    return jsonify(ok=True,count=len(data),sets=data)

@app.post("/api/kr-catalog/check")
def kr_catalog_check():
    body=request.get_json(silent=True) or {}
    nums=[re.sub(r"[^0-9]","",str(x)) for x in body.get("numbers",[])]
    cat=load_kr_catalog()
    return jsonify(items={n:cat.get(n) for n in nums if n})

@app.post("/api/auto-sync")
def auto_sync():
    body=request.get_json(silent=True) or {}
    nums=[]
    for x in body.get("numbers",[]):
        n=re.sub(r"[^0-9]","",str(x))
        if n and n not in nums: nums.append(n)
    cat=load_kr_catalog()
    out={}
    if not KEY:
        return jsonify(ok=False,error="BRICKSET_API_KEY not configured"),503
    for n in nums[:100]:
        try:
            params=json.dumps({"setNumber":n+"-1","pageSize":1})
            r=requests.get(API+"/getSets",params={"apiKey":KEY,"userHash":"","params":params},timeout=15)
            d=r.json()
            s=(d.get("sets") or [None])[0]
            if not s: continue
            k=cat.get(n) or {}
            out[n]={
                "number":n,
                "name":s.get("name"),
                "year":s.get("year"),
                "theme":s.get("theme"),
                "pieces":s.get("pieces"),
                "image":(s.get("image") or {}).get("imageURL") or (s.get("image") or {}).get("thumbnailURL"),
                "LEGOCom":s.get("LEGOCom"),
                "kr":k or None
            }
        except Exception:
            continue
    return jsonify(ok=True,items=out,catalog_count=len(cat))

LIVE_KR_CACHE={}
LIVE_KR_TTL=21600

def _clean_text(v):
    if not v: return ""
    v=re.sub(r"<[^>]+>"," ",str(v))
    v=v.replace("\\u0026","&").replace("\\u003c","<").replace("\\u003e",">")
    try: v=bytes(v,"utf-8").decode("unicode_escape") if "\\u" in v else v
    except: pass
    return re.sub(r"\s+"," ",v).strip()

def _krw_from_text(t):
    pats=[
      r'"price"\s*:\s*"?([0-9]{4,7})"?',
      r'"priceCentAmount"\s*:\s*([0-9]{4,9})',
      r'([0-9]{1,3}(?:,[0-9]{3})+)\s*원'
    ]
    for p in pats:
        for m in re.finditer(p,t,re.I):
            raw=m.group(1).replace(",","")
            try:
                x=int(raw)
                if "priceCentAmount" in p and x>1000000: x//=100
                if 5000 <= x <= 3000000: return x
            except: pass
    return None

def _official_kr_lookup(number):
    """LEGO Korea 공식 제품 페이지에서 한글명과 원화 정가를 찾는다.
    현재 판매/품절/단종 페이지 모두 대상으로 하며, 찾은 값만 반환한다.
    """
    n=str(number)
    headers={
      "User-Agent":"Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/143 Safari/537.36",
      "Accept-Language":"ko-KR,ko;q=0.9,en;q=0.7"
    }

    # LEGO 제품 URL은 slug를 몰라도 /product/x-SETNO 형태가 제품으로 연결되는 경우가 많다.
    # 실패하면 한국 사이트 검색 페이지도 확인한다.
    urls=[
      f"https://www.lego.com/ko-kr/product/x-{n}",
      f"https://www.lego.com/ko-kr/search?q={n}",
    ]
    for url in urls:
        try:
            r=requests.get(url,headers=headers,timeout=15,allow_redirects=True)
            if not r.ok: continue
            t=r.text
            final_url=r.url

            # 다른 세트 검색 결과가 섞이는 것을 방지.
            if n not in t and n not in final_url:
                continue

            name=None
            for pat in [
                r'<h1[^>]*>(.*?)</h1>',
                r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
                r'"productName"\s*:\s*"([^"]+)"',
                r'"name"\s*:\s*"([^"]+)"'
            ]:
                for x in re.findall(pat,t,re.I|re.S):
                    candidate=_clean_lego_title(x,n)
                    if candidate:
                        name=candidate
                        break
                if name: break

            # KRW 가격: JSON/구조화 데이터 우선, 화면 텍스트는 보조.
            price=None
            price_patterns=[
                r'"price"\s*:\s*"?([0-9]{4,7})"?\s*,\s*"priceCurrency"\s*:\s*"KRW"',
                r'"priceCurrency"\s*:\s*"KRW"\s*,\s*"price"\s*:\s*"?([0-9]{4,7})"?',
                r'"formattedValue"\s*:\s*"₩?\s*([0-9,]{4,10})"',
                r'([0-9]{1,3}(?:,[0-9]{3})+)\s*원'
            ]
            for pat in price_patterns:
                mm=re.search(pat,t,re.I)
                if mm:
                    try:
                        v=int(mm.group(1).replace(",",""))
                        if 1000 <= v <= 10000000:
                            price=v
                            break
                    except Exception:
                        pass

            if name or price:
                return {"name_ko":name,"price":price,"currency":"KRW",
                        "source":"LEGO Korea","source_url":final_url,
                        "checked_at":time.strftime("%Y-%m-%d")}
        except Exception:
            continue
    return None

@app.post("/api/kr-live-sync")
def kr_live_sync():
    body=request.get_json(silent=True) or {}
    nums=[]
    for x in body.get("numbers",[]):
        n=re.sub(r"[^0-9]","",str(x))
        if n and n not in nums: nums.append(n)
    cat=load_kr_catalog()
    out={}
    for n in nums[:50]:
        # Verified catalog always wins; live official lookup fills missing sets.
        k=cat.get(n)
        if not k: k=_official_kr_lookup(n)
        out[n]=k
    return jsonify(ok=True,items=out,verified_catalog_count=len(cat),
                   live_cache_count=len(LIVE_KR_CACHE))

DISCOVERED_KR={}
DISCOVERY_TS=0
DISCOVERY_TTL=43200

def _extract_products_from_lego_html(t):
    out={}
    # Product URLs normally end with a numeric LEGO set number.
    links=list(re.finditer(r'href=["\']([^"\']*/product/[^"\']*?-(\d{4,6})(?:["\']|\?))',t,re.I))
    for m in links:
        n=m.group(2)
        a=max(0,m.start()-1400); b=min(len(t),m.end()+2200)
        chunk=t[a:b]
        # Prefer nearby heading/title text.
        names=[]
        for p in [r'<h[23][^>]*>(.*?)</h[23]>',r'"name"\s*:\s*"([^"]+)"']:
            names += re.findall(p,chunk,re.I|re.S)
        name=None
        for x in names:
            x=_clean_text(x)
            if x and len(x)>2 and not x.isdigit() and "전체 상품" not in x:
                name=x; break
        price=_krw_from_text(chunk)
        if name or price:
            out[n]={"name_ko":name,"price":price,"currency":"KRW",
                    "source":"LEGO Korea auto","checked_at":time.strftime("%Y-%m-%d"),
                    "source_url":"https://www.lego.com"+m.group(1) if m.group(1).startswith("/") else m.group(1)}
    return out

def refresh_discovered_kr(force=False):
    global DISCOVERY_TS, DISCOVERED_KR
    now=time.time()
    if not force and DISCOVERED_KR and now-DISCOVERY_TS<DISCOVERY_TTL:
        return DISCOVERED_KR
    headers={"User-Agent":"Mozilla/5.0 (compatible; LEGOCollector/1.0)",
             "Accept-Language":"ko-KR,ko;q=0.9,en;q=0.5"}
    found={}
    # New products first: recent additions are the most important to discover automatically.
    urls=["https://www.lego.com/ko-kr/categories/new-sets-and-products"]
    # A few leading all-set pages catch current catalogue changes without a long 40+ page request.
    urls += ["https://www.lego.com/ko-kr/categories/all-sets?page="+str(i) for i in range(1,7)]
    for url in urls:
        try:
            r=requests.get(url,headers=headers,timeout=10)
            if r.ok: found.update(_extract_products_from_lego_html(r.text))
        except: pass
    if found:
        DISCOVERED_KR.update(found)
        DISCOVERY_TS=now
    return DISCOVERED_KR

@app.post("/api/catalog-auto-refresh")
def catalog_auto_refresh():
    body=request.get_json(silent=True) or {}
    nums=[]
    for x in body.get("numbers",[]):
        n=re.sub(r"[^0-9]","",str(x))
        if n and n not in nums: nums.append(n)
    verified=load_kr_catalog()
    discovered=refresh_discovered_kr(False)
    items={}
    for n in nums[:100]:
        k=verified.get(n) or discovered.get(n)
        if not k: k=_official_kr_lookup(n)
        items[n]=k
    return jsonify(ok=True,items=items,verified_count=len(verified),
                   discovered_count=len(discovered),
                   total_available=len(set(verified)|set(discovered)),
                   refreshed_at=time.strftime("%Y-%m-%d %H:%M:%S"))

SUPABASE_URL=os.environ.get("SUPABASE_URL","").rstrip("/")
SUPABASE_SERVICE_KEY=os.environ.get("SUPABASE_SERVICE_KEY","")
SUPABASE_TABLE=os.environ.get("SUPABASE_TABLE","lego_kr_catalog")

def _sb_headers(prefer=None):
    h={"apikey":SUPABASE_SERVICE_KEY,"Content-Type":"application/json"}
    # Legacy service_role keys are JWTs and support Authorization: Bearer.
    # New sb_secret_ keys are sent as the apikey header and must never be exposed to the browser.
    if SUPABASE_SERVICE_KEY and not SUPABASE_SERVICE_KEY.startswith("sb_secret_"):
        h["Authorization"]="Bearer "+SUPABASE_SERVICE_KEY
    if prefer: h["Prefer"]=prefer
    return h

def sb_enabled():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

def sb_get(numbers, diagnostic=False):
    info={"attempted":False,"ok":False,"status":None,"error":None,"rows":0}
    if not sb_enabled() or not numbers:
        info["error"]="Supabase environment variables missing" if not sb_enabled() else "No set numbers"
        return ({},info) if diagnostic else {}
    try:
        vals=",".join(numbers)
        info["attempted"]=True
        r=requests.get(f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
          headers=_sb_headers(),params={"select":"*","set_number":f"in.({vals})"},timeout=12)
        info["status"]=r.status_code
        info["ok"]=r.ok
        if not r.ok:
            info["error"]=(r.text or "")[:500]
            return ({},info) if diagnostic else {}
        rows=r.json()
        info["rows"]=len(rows)
        data={str(x["set_number"]):x for x in rows}
        return (data,info) if diagnostic else data
    except Exception as e:
        info["error"]=str(e)[:500]
        return ({},info) if diagnostic else {}

def sb_upsert(rows, diagnostic=False):
    info={"attempted":False,"ok":False,"status":None,"error":None,"rows":len(rows or [])}
    if not sb_enabled() or not rows:
        info["error"]="Supabase environment variables missing" if not sb_enabled() else "No rows to save"
        return info if diagnostic else False
    try:
        info["attempted"]=True
        r=requests.post(f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
          headers=_sb_headers("resolution=merge-duplicates,return=minimal"),
          params={"on_conflict":"set_number"},json=rows,timeout=15)
        info["status"]=r.status_code
        info["ok"]=r.ok
        if not r.ok: info["error"]=(r.text or "")[:500]
        return info if diagnostic else r.ok
    except Exception as e:
        info["error"]=str(e)[:500]
        return info if diagnostic else False

@app.get("/api/supabase-diagnostic")
def supabase_diagnostic():
    result={
        "ok":False,
        "configured":sb_enabled(),
        "url_configured":bool(SUPABASE_URL),
        "key_configured":bool(SUPABASE_SERVICE_KEY),
        "key_type":"new_secret" if SUPABASE_SERVICE_KEY.startswith("sb_secret_") else ("legacy_or_other" if SUPABASE_SERVICE_KEY else "missing"),
        "table":SUPABASE_TABLE
    }
    if not sb_enabled():
        result["error"]="SUPABASE_URL or SUPABASE_SERVICE_KEY missing"
        return jsonify(result),200
    _,read_info=sb_get(["10300"],diagnostic=True)
    result["read"]=read_info
    # Seed the verified 10300 row as a safe write test; no secret is returned.
    seed=load_kr_catalog().get("10300")
    if seed:
        row={"set_number":"10300","name_ko":seed.get("name_ko"),"price_krw":seed.get("price"),
             "source":seed.get("source"),"source_url":seed.get("source_url"),"checked_at":seed.get("checked_at")}
        result["write"]=sb_upsert([row],diagnostic=True)
    result["ok"]=bool(result.get("read",{}).get("ok") and result.get("write",{}).get("ok"))
    return jsonify(result),200

def _clean_lego_title(s, number):
    if not s: return None
    s=re.sub(r"<[^>]+>"," ",str(s))
    s=s.replace("&amp;","&").replace("&quot;",'"').replace("&#39;","'")
    s=re.sub(r"\\s+"," ",s).strip(" -|")
    s=re.sub(r"\\s*\\|\\s*LEGO.*$","",s,flags=re.I)
    s=re.sub(r"\\s*-\\s*조립 설명서.*$","",s)
    s=re.sub(r"^\\s*조립 설명서\\s*[-–:]\\s*","",s)
    s=re.sub(rf"^\\s*{re.escape(str(number))}\\s*","",s)
    s=s.strip()
    if not re.search(r"[가-힣]",s): return None
    return s if 1 < len(s) < 120 else None

def _instruction_name(number):
    """LEGO Korea 공식 조립설명서/검색 페이지에서 한국어 제품명을 찾는다."""
    urls=[
      f"https://www.lego.com/ko-kr/service/building-instructions/{number}",
      f"https://www.lego.com/ko-kr/service/building-instructions/search-results?page=1&searchString={number}",
      f"https://www.lego.com/ko-kr/service/buildinginstructions/{number}",
    ]
    headers={"User-Agent":"Mozilla/5.0 AppleWebKit/537.36 Chrome/143 Safari/537.36",
             "Accept-Language":"ko-KR,ko;q=0.9,en;q=0.7"}
    for url in urls:
        try:
            r=requests.get(url,headers=headers,timeout=15,allow_redirects=True)
            if not r.ok: continue
            t=r.text
            # h1
            for x in re.findall(r"<h1[^>]*>(.*?)</h1>",t,re.I|re.S):
                name=_clean_lego_title(x,number)
                if name: return name,url
            # metadata/title
            pats=[
              r'<meta[^>]+property=["\\\']og:title["\\\'][^>]+content=["\\\']([^"\\\']+)',
              r'<meta[^>]+content=["\\\']([^"\\\']+)["\\\'][^>]+property=["\\\']og:title["\\\']',
              r"<title[^>]*>(.*?)</title>"
            ]
            for pat in pats:
                for x in re.findall(pat,t,re.I|re.S):
                    name=_clean_lego_title(x,number)
                    if name: return name,url
            # visible search result, e.g. "10305 사자 기사의 성"
            for x in re.findall(rf'{re.escape(str(number))}\\s+([^"<>{{}}]{{2,100}})',t):
                name=_clean_lego_title(x,number)
                if name: return name,url
            # JSON fields
            for x in re.findall(r'"(?:name|title|productName)"\\s*:\\s*"([^"]*[가-힣][^"]*)"',t,re.I):
                name=_clean_lego_title(x,number)
                if name: return name,url
        except Exception:
            continue
    return None,None

def _official_kr_lookup(number):
    now=time.time()
    c=LIVE_KR_CACHE.get(number)
    if c and now-c["ts"]<LIVE_KR_TTL: return c["data"]
    headers={"User-Agent":"Mozilla/5.0 (compatible; LEGOCollector/1.0)","Accept-Language":"ko-KR,ko;q=0.9,en;q=0.5"}
    urls=[
      "https://www.lego.com/ko-kr/search?q="+number,
      "https://www.lego.com/ko-kr/service/building-instructions/"+number
    ]
    result=None
    for url in urls:
        try:
            t=requests.get(url,headers=headers,timeout=12).text
            if number not in t: continue
            name=None
            for p in [
              r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
              r'"productName"\s*:\s*"([^"]+)"',
              r'"name"\s*:\s*"([^"]+)"'
            ]:
                mm=re.search(p,t,re.I)
                if mm:
                    cand=_clean_text(mm.group(1))
                    if cand and len(cand)>2 and "LEGO" not in cand.upper():
                        name=cand; break
            price=_krw_from_text(t)
            if name or price:
                result={"name_ko":name,"price":price,"currency":"KRW","source":"LEGO Korea live",
                        "checked_at":time.strftime("%Y-%m-%d"),"source_url":url}
                break
        except: pass
    LIVE_KR_CACHE[number]={"ts":now,"data":result}
    return result


SUPABASE_URL=os.environ.get("SUPABASE_URL","").rstrip("/")
SUPABASE_SERVICE_KEY=os.environ.get("SUPABASE_SERVICE_KEY","")
SUPABASE_TABLE=os.environ.get("SUPABASE_TABLE","lego_kr_catalog")

def _sb_headers(prefer=None):
    h={"apikey":SUPABASE_SERVICE_KEY,"Content-Type":"application/json"}
    # Legacy service_role keys are JWTs and support Authorization: Bearer.
    # New sb_secret_ keys are sent as the apikey header and must never be exposed to the browser.
    if SUPABASE_SERVICE_KEY and not SUPABASE_SERVICE_KEY.startswith("sb_secret_"):
        h["Authorization"]="Bearer "+SUPABASE_SERVICE_KEY
    if prefer: h["Prefer"]=prefer
    return h

def sb_enabled():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

def sb_get(numbers, diagnostic=False):
    info={"attempted":False,"ok":False,"status":None,"error":None,"rows":0}
    if not sb_enabled() or not numbers:
        info["error"]="Supabase environment variables missing" if not sb_enabled() else "No set numbers"
        return ({},info) if diagnostic else {}
    try:
        vals=",".join(numbers)
        info["attempted"]=True
        r=requests.get(f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
          headers=_sb_headers(),params={"select":"*","set_number":f"in.({vals})"},timeout=12)
        info["status"]=r.status_code
        info["ok"]=r.ok
        if not r.ok:
            info["error"]=(r.text or "")[:500]
            return ({},info) if diagnostic else {}
        rows=r.json()
        info["rows"]=len(rows)
        data={str(x["set_number"]):x for x in rows}
        return (data,info) if diagnostic else data
    except Exception as e:
        info["error"]=str(e)[:500]
        return ({},info) if diagnostic else {}

def sb_upsert(rows, diagnostic=False):
    info={"attempted":False,"ok":False,"status":None,"error":None,"rows":len(rows or [])}
    if not sb_enabled() or not rows:
        info["error"]="Supabase environment variables missing" if not sb_enabled() else "No rows to save"
        return info if diagnostic else False
    try:
        info["attempted"]=True
        r=requests.post(f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
          headers=_sb_headers("resolution=merge-duplicates,return=minimal"),
          params={"on_conflict":"set_number"},json=rows,timeout=15)
        info["status"]=r.status_code
        info["ok"]=r.ok
        if not r.ok: info["error"]=(r.text or "")[:500]
        return info if diagnostic else r.ok
    except Exception as e:
        info["error"]=str(e)[:500]
        return info if diagnostic else False


def _instruction_name(number):
    # LEGO Korea building-instructions pages are a stronger source for official Korean names,
    # including retired sets.
    headers={"User-Agent":"Mozilla/5.0 (compatible; LEGOCollector/1.0)",
             "Accept-Language":"ko-KR,ko;q=0.9,en;q=0.5"}
    urls=[
      f"https://www.lego.com/ko-kr/service/building-instructions/{number}",
      f"https://www.lego.com/ko-kr/service/building-instructions/search-results?page=1&searchString={number}"
    ]
    for url in urls:
        try:
            t=requests.get(url,headers=headers,timeout=12).text
            # Exact-number result/card or page H1.
            pats=[
              r'<h1[^>]*>(.*?)</h1>',
              rf'{re.escape(number)}\s+([^<"\n]{{2,120}})'
            ]
            for p in pats:
                for m in re.finditer(p,t,re.I|re.S):
                    name=_clean_text(m.group(1))
                    if name and name not in ("조립 설명서","검색 결과") and len(name)<140:
                        return name,url
        except: pass
    return None,None

@app.get("/api/kr-lookup/<number>")
def kr_lookup_debug(number):
    n=re.sub(r"[^0-9]","",str(number))
    if not n: return jsonify(ok=False,error="invalid set number"),400

    verified=load_kr_catalog().get(n) or {}
    stored=(sb_get([n]).get(n) if sb_enabled() else None) or {}
    instruction_name,instruction_url=_instruction_name(n)
    live=_official_kr_lookup(n) or {}

    name=(live.get("name_ko") or instruction_name or verified.get("name_ko")
          or stored.get("name_ko"))
    price=(live.get("price") if live.get("price") is not None else
           verified.get("price") if verified.get("price") is not None else
           stored.get("price_krw"))
    source_url=live.get("source_url") or instruction_url or verified.get("source_url") or stored.get("source_url")
    source="LEGO Korea" if (live or instruction_name) else (verified.get("source") or stored.get("source"))
    item=None
    if name or price is not None:
        item={"name_ko":name,"price":price,"currency":"KRW","source":source,
              "source_url":source_url,"checked_at":time.strftime("%Y-%m-%d")}
        if sb_enabled():
            sb_upsert([{"set_number":n,"name_ko":name,"price_krw":price,
                        "source":source,"source_url":source_url,
                        "checked_at":item["checked_at"]}])
    return jsonify(ok=bool(item),number=n,item=item,
                   official_name_found=bool(instruction_name or live.get("name_ko")),
                   official_price_found=live.get("price") is not None,
                   official_url=source_url,persistent=sb_enabled())


@app.post("/api/kr-collect")
def kr_collect():
    data=request.get_json(silent=True) or {}
    nums=data.get("numbers") or []
    nums=[re.sub(r"[^0-9]","",str(x)) for x in nums][:20]
    nums=[x for x in nums if x]
    results={}
    for n in nums:
        verified=load_kr_catalog().get(n) or {}
        stored=(sb_get([n]).get(n) if sb_enabled() else None) or {}
        instruction_name,instruction_url=_instruction_name(n)
        live=_official_kr_lookup(n) or {}
        name=live.get("name_ko") or instruction_name or verified.get("name_ko") or stored.get("name_ko")
        price=(live.get("price") if live.get("price") is not None else
               verified.get("price") if verified.get("price") is not None else stored.get("price_krw"))
        if name or price is not None:
            item={"name_ko":name,"price":price,"currency":"KRW",
                  "source":"LEGO Korea" if (live or instruction_name) else (verified.get("source") or stored.get("source")),
                  "source_url":live.get("source_url") or instruction_url or verified.get("source_url") or stored.get("source_url"),
                  "checked_at":time.strftime("%Y-%m-%d")}
            results[n]=item
            if sb_enabled():
                sb_upsert([{"set_number":n,"name_ko":name,"price_krw":price,
                            "source":item["source"],"source_url":item["source_url"],
                            "checked_at":item["checked_at"]}])
    return jsonify(ok=True,items=results,count=len(results))

@app.post("/api/persistent-catalog-sync")
def persistent_catalog_sync():
    body=request.get_json(silent=True) or {}
    nums=[]
    for x in body.get("numbers",[]):
        n=re.sub(r"[^0-9]","",str(x))
        if n and n not in nums: nums.append(n)
    nums=nums[:100]
    verified=load_kr_catalog()
    stored,read_diag=sb_get(nums,diagnostic=True)
    out={}
    to_save=[]
    for n in nums:
        # Priority: verified repo catalog -> persistent DB -> official live sources.
        k=verified.get(n)
        if k:
            # v22 fix: verified catalog rows are also persisted to Supabase.
            to_save.append({"set_number":n,"name_ko":k.get("name_ko"),
                "price_krw":k.get("price"),"source":k.get("source"),
                "source_url":k.get("source_url"),"checked_at":k.get("checked_at")})
        elif n in stored:
            x=stored[n]
            k={"name_ko":x.get("name_ko"),"price":x.get("price_krw"),
               "currency":"KRW","source":x.get("source") or "persistent DB",
               "checked_at":x.get("checked_at"),"source_url":x.get("source_url")}
        else:
            name,url=_instruction_name(n)
            live=_official_kr_lookup(n) or {}
            k=None
            if name or live.get("price"):
                k={"name_ko":name or live.get("name_ko"),"price":live.get("price"),
                   "currency":"KRW","source":"LEGO Korea",
                   "checked_at":time.strftime("%Y-%m-%d"),
                   "source_url":url or live.get("source_url")}
                to_save.append({"set_number":n,"name_ko":k.get("name_ko"),
                    "price_krw":k.get("price"),"source":k.get("source"),
                    "source_url":k.get("source_url"),"checked_at":k.get("checked_at")})
        out[n]=k
    write_diag=sb_upsert(to_save,diagnostic=True)
    return jsonify(ok=True,items=out,persistent=sb_enabled(),
                   saved=bool(write_diag.get("ok")),db_hits=len(stored),
                   new_items=len(to_save),verified_count=len(verified),
                   total_available=len(set(verified)|set(stored)|set(k for k,v in out.items() if v)),
                   supabase={"read":read_diag,"write":write_diag})

