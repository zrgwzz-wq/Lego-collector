import os, time,json,time,requests,re
from flask import Flask,request,jsonify,send_from_directory
app=Flask(__name__); KEY=os.environ.get("BRICKSET_API_KEY","")
API="https://brickset.com/api/v3.asmx"
CACHE={}; TTL=21600
@app.get("/api/kr-overlay/<number>")
def api_kr_overlay(number):
    n=re.sub(r"[^0-9]","",str(number))
    if not n:
        return jsonify(ok=False,error="invalid set number"),400

    # v38.3: use exactly the same proven pipeline as /api/kr-lookup.
    item,diag=_merge_kr_sources(n)
    if not item:
        return jsonify(ok=False,number=n,name_ko=None,price=None,currency="KRW",
                       name_source=None,price_source=None,
                       diagnostics=diag,validation="brickset-ko-overlay-v38.3")

    name=item.get("name_ko")
    price=item.get("price")
    source=item.get("source")
    # Current DB schema has one source field; expose it conservatively.
    name_source=source if name else None
    price_source=source if price is not None else None

    if sb_enabled():
        sb_upsert([{"set_number":n,"name_ko":name,"price_krw":price,
                    "source":source,"source_url":item.get("source_url"),
                    "checked_at":item.get("checked_at")}])

    return jsonify(ok=True,number=n,name_ko=name,price=price,
                   currency=item.get("currency") or "KRW",
                   name_source=name_source,price_source=price_source,
                   diagnostics=diag,validation="brickset-ko-overlay-v38.3")


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
        version="v38.3"
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

def _extract_krw(text, labels=("발매가","정가","출시가")):
    plain=re.sub(r"<[^>]+>"," ",text or "")
    plain=re.sub(r"\s+"," ",plain)
    for label in labels:
        m=re.search(re.escape(label)+r"\s*[:：]?\s*₩?\s*([0-9]{1,3}(?:,[0-9]{3})+)\s*원?",plain,re.I)
        if m:
            try: return int(m.group(1).replace(",",""))
            except: pass
    return None

BAD_KR_NAME_MARKERS=("product_style_code","product_url","검색","로그인","회원가입","구매","판매","관심상품","고객센터","이벤트","kream","danawa","javascript","http://","https://")
def _safe_kr_product_name(value, number):
    if not value: return None
    x=re.sub(r"<[^>]+>"," ",str(value))
    x=re.sub(r"\\[nrt]"," ",x)
    x=re.sub(r"\s+"," ",x).strip(" -|:;,")
    lo=x.lower()
    if _bad_page_text(x) or any(m.lower() in lo for m in BAD_KR_NAME_MARKERS): return None
    if not re.search(r"[가-힣]",x) or len(x)<2 or len(x)>80: return None
    if x.count('"')>1 or "{" in x or "}" in x or x.count(":")>3: return None
    return x
def _safe_price(value):
    try:
        v=int(value); return v if 1000<=v<=10000000 else None
    except: return None

def _decode_jsonish(text):
    if not text: return ""
    try:
        text=re.sub(r"\\u([0-9a-fA-F]{4})",lambda m: chr(int(m.group(1),16)),text)
    except Exception: pass
    return text.replace("\\/","/").replace('\\"','"')

def _detail_page_metadata(html, number):
    """Strict detail parser: exact set number + structured title; price only from launch-price labels."""
    n=str(number)
    if not html or _bad_page_text(html[:5000]): return (None,None)
    text=_decode_jsonish(html)
    if not re.search(rf"(?<!\d){re.escape(n)}(?!\d)",text): return (None,None)

    # Price must be attached to a launch/MSRP label, not a generic sale/current price.
    price=None
    price_patterns=[
        r'(?:발매가|출시가|정가|retail_price|release_price|original_price)\s*["\']?\s*[:：=]\s*["\']?\s*(?:₩|KRW)?\s*([0-9]{4,8}|[0-9]{1,3}(?:,[0-9]{3})+)',
        r'(?:발매가|출시가|정가)[^0-9]{0,40}(?:₩\s*)?([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,8})\s*원?'
    ]
    for pat in price_patterns:
        m=re.search(pat,text,re.I)
        if m:
            price=_safe_price(m.group(1).replace(",",""))
            if price is not None: break

    # Product title only from structured/detail title fields.
    candidates=[]
    title_patterns=[
        r'"(?:translated_name|local_name|name_ko|product_name|name)"\s*:\s*"([^"]{2,160})"',
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r'<h1[^>]*>(.*?)</h1>'
    ]
    for pat in title_patterns:
        for m in re.finditer(pat,text,re.I|re.S):
            c=re.sub(r"<[^>]+>"," ",m.group(1))
            if n in c or re.search(r"[가-힣]",c):
                candidates.append(c)

    name=None
    for c in candidates:
        c=re.sub(rf"(?<!\d){re.escape(n)}(?!\d)"," ",c)
        c=re.sub(r"(?i)\bLEGO\b|레고"," ",c)
        c=re.sub(r"\s+"," ",c).strip(" -|·:")
        c=_safe_kr_product_name(c,n)
        if c:
            name=c
            break
    return name,price

def _extract_detail_links(html, base, number, allowed_host):
    """Search pages are discovery only; their text is never saved as product metadata."""
    from urllib.parse import urljoin, urlparse
    n=str(number); links=[]
    for href in re.findall(r"href=[\\\"']([^\\\"']+)[\\\"']",html or "",re.I):
        u=urljoin(base,href.replace("&amp;","&"))
        try:
            host=urlparse(u).netloc.lower()
        except: continue
        if allowed_host not in host: continue
        # Product-ish URL only. Exact number may be in URL or nearby page search can still lead to detail.
        if any(x in u.lower() for x in ("/products/","/product/","goods","detail")):
            if u not in links: links.append(u)
        if len(links)>=8: break
    return links

def _kream_kr_lookup(number):
    n=str(number); search=f"https://kream.co.kr/search?keyword={n}"
    h={"User-Agent":"Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/143 Mobile Safari/537.36","Accept-Language":"ko-KR,ko;q=0.9"}
    try:
        r=requests.get(search,headers=h,timeout=12)
        if not r.ok or _bad_page_text(r.text[:5000]): return None
        for u in _extract_detail_links(r.text,r.url,n,"kream.co.kr"):
            try:
                d=requests.get(u,headers=h,timeout=12)
                if not d.ok: continue
                _name,price=_detail_page_metadata(d.text,n)
                if price is not None:
                    return {"name_ko":None,"price":price,"currency":"KRW","source":"KREAM 상세 발매정보 v32 (가격 전용)","source_url":d.url,"checked_at":time.strftime("%Y-%m-%d")}
            except Exception: continue
    except Exception: pass
    return None

def _danawa_kr_lookup(number):
    """v33: Danawa is not trusted as a Korean-name source.
    Keep a safe probe for diagnostics only; never return page/UI text as product metadata.
    """
    n=str(number); search=f"https://search.danawa.com/mobile/dsearch.php?keyword={n}"
    h={"User-Agent":"Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/143 Mobile Safari/537.36",
       "Accept-Language":"ko-KR,ko;q=0.9"}
    try:
        r=requests.get(search,headers=h,timeout=12)
        if not r.ok or _bad_page_text(r.text[:5000]): return None
        # Deliberately no name extraction. Search HTML is not authoritative enough.
        return None
    except Exception:
        return None

def sb_delete_bad(set_number):
    if not sb_enabled(): return False
    try:
        url=SUPABASE_URL.rstrip("/")+"/rest/v1/lego_kr_catalog?set_number=eq."+str(set_number)
        r=requests.delete(url,headers=_sb_headers(),timeout=10)
        return r.status_code in (200,204)
    except Exception:
        return False

def _merge_kr_sources(number):
    """Return merged metadata + diagnostics. Price priority:
    LEGO Korea -> verified repo -> KREAM launch price -> BrickMecha launch price.
    Names may additionally come from LEGO instructions or Danawa.
    """
    n=str(number)
    verified=load_kr_catalog().get(n) or {}
    stored=(sb_get([n]).get(n) if sb_enabled() else None) or {}
    original_stored_name=stored.get("name_ko")
    stored_source=str(stored.get("source") or "")
    untrusted_stored_name=bool(original_stored_name and ("KREAM" in stored_source or "다나와" in stored_source))
    if stored.get("name_ko"):
        stored["name_ko"]=_safe_kr_product_name(stored.get("name_ko"),n)
    if stored.get("price_krw") is not None:
        stored["price_krw"]=_safe_price(stored.get("price_krw"))
    if untrusted_stored_name:
        stored["name_ko"]=None
        # Preserve a valid price, but erase the untrusted name in Supabase.
        if stored.get("price_krw") is not None and sb_enabled():
            sb_upsert([{"set_number":n,"name_ko":None,"price_krw":stored.get("price_krw"),
                        "source":stored.get("source"),"source_url":stored.get("source_url"),
                        "checked_at":stored.get("checked_at")}])
        elif stored.get("price_krw") is None:
            sb_delete_bad(n)
            stored={}
    elif original_stored_name and not stored.get("name_ko") and stored.get("price_krw") is None:
        sb_delete_bad(n)
        stored={}

    instruction_name,instruction_url=_instruction_name(n)
    official=_official_kr_lookup(n) or {}
    if official.get("name_ko") and not _valid_kr_name(official.get("name_ko")):
        official["name_ko"]=None
    brick=_kr_catalog_fallback(n) or {}
    kream=_kream_kr_lookup(n) or {}
    danawa=_danawa_kr_lookup(n) or {}
    for candidate in (official,brick,kream,danawa):
        if candidate.get("name_ko"): candidate["name_ko"]=_safe_kr_product_name(candidate.get("name_ko"),n)
        if candidate.get("price") is not None: candidate["price"]=_safe_price(candidate.get("price"))

    name=(official.get("name_ko") or instruction_name or verified.get("name_ko")
          or brick.get("name_ko") or stored.get("name_ko"))
    price=(official.get("price") if official.get("price") is not None else
           verified.get("price") if verified.get("price") is not None else
           kream.get("price") if kream.get("price") is not None else
           brick.get("price") if brick.get("price") is not None else
           stored.get("price_krw"))

    chosen = (official if (official.get("name_ko") or official.get("price") is not None) else
              verified if verified else kream if kream else brick if brick else
              danawa if danawa else {})
    source=chosen.get("source") or ("LEGO Korea 조립 설명서" if instruction_name else stored.get("source"))
    source_url=chosen.get("source_url") or instruction_url or stored.get("source_url")
    item=None
    if name or price is not None:
        item={"name_ko":name,"price":price,"currency":"KRW","source":source,
              "source_url":source_url,"checked_at":time.strftime("%Y-%m-%d")}
    def usable(x): return bool(x and (x.get("name_ko") or x.get("price") is not None))
    diag={"official":usable(official),"instructions":bool(instruction_name),
          "kream":usable(kream),"brickmecha":usable(brick),"danawa":usable(danawa),
          "stored":bool(stored.get("name_ko") or stored.get("price_krw") is not None),
          "verified":bool(verified),"validation":"brickset-ko-overlay-v38.3"}
    return item,diag

def _kr_catalog_fallback(number):
    """Fallback for retired Korean sets when LEGO Korea blocks Render.
    Uses a Korean catalog page that exposes set number, Korean name and original retail price.
    """
    n=str(number)
    url=f"https://www.brickmecha.net/lego-instructions.php?lego-instructions-page-no=1&lego-set-instructions-book-no=1&lego-set-no={n}&lng=ko&page="
    headers={"User-Agent":"Mozilla/5.0 AppleWebKit/537.36 Chrome/143 Safari/537.36",
             "Accept-Language":"ko-KR,ko;q=0.9,en;q=0.7"}
    try:
        r=requests.get(url,headers=headers,timeout=12)
        if not r.ok or n not in r.text:
            return None
        t=r.text
        # Strip tags for stable Korean label parsing.
        plain=re.sub(r"<script.*?</script>|<style.*?</style>"," ",t,flags=re.I|re.S)
        plain=re.sub(r"<[^>]+>"," ",plain)
        plain=re.sub(r"&nbsp;"," ",plain)
        plain=re.sub(r"\s+"," ",plain)

        name=None
        m=re.search(r"레고\s*제품명\s*[:：]?\s*(.{1,100}?)(?=부품(?:합계)?|발매\s*당시|레고\s*제품번호|$)",plain)
        if m:
            name=m.group(1).strip(" :-")
        if not name:
            # Page title commonly starts with the Korean set name followed by the number.
            m=re.search(rf"([가-힣A-Za-z0-9®™·&'’\-\s]{{2,80}}?)\s+{re.escape(n)}\s+레고",plain)
            if m and re.search(r"[가-힣]",m.group(1)):
                name=m.group(1).strip()

        price=None
        m=re.search(r"발매\s*당시\s*판매가\s*[:：]?\s*([0-9,]+)\s*원",plain)
        if m:
            try: price=int(m.group(1).replace(",",""))
            except: pass

        if name or price:
            return {"name_ko":name,"price":price,"currency":"KRW",
                    "source":"BrickMecha 한국 카탈로그",
                    "source_url":r.url,"checked_at":time.strftime("%Y-%m-%d")}
    except Exception:
        pass
    return None

def _lego_catalog_scan(target_number=None):
    """v35: LEGO Korea current catalog discovery, exact-set detail verification."""
    target=str(target_number) if target_number else None
    h={"User-Agent":"Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/143 Mobile Safari/537.36",
       "Accept-Language":"ko-KR,ko;q=0.9,en;q=0.7"}
    found={}
    for base in ["https://www.lego.com/ko-kr/categories/all-sets",
                 "https://www.lego.com/ko-kr/categories/new-sets-and-products"]:
        for page in range(1,7):
            u=base+(f"?page={page}" if page>1 else "")
            try:
                r=requests.get(u,headers=h,timeout=18,allow_redirects=True)
                if not r.ok or _bad_page_text(r.text[:5000]): break
                links=re.findall(r'href=["\']([^"\']*/product/[^"\']+?-([0-9]{4,7})(?:[/?#][^"\']*)?)["\']',r.text,re.I)
                if not links: break
                for href,num in links:
                    if target and num!=target: continue
                    if num in found: continue
                    href=href.replace("&amp;","&")
                    if href.startswith("/"): href="https://www.lego.com"+href
                    href=href.split("?")[0].split("#")[0]
                    try:
                        d=requests.get(href,headers=h,timeout=15,allow_redirects=True)
                        if not d.ok or _bad_page_text(d.text[:5000]): continue
                        dt=d.text
                        if num not in dt and num not in d.url: continue
                        name=None
                        for pat in [r'<h1[^>]*>(.*?)</h1>',r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',r'"productName"\s*:\s*"([^"]+)"']:
                            for x in re.findall(pat,dt,re.I|re.S):
                                c=_clean_lego_title(x,num)
                                if c and _valid_kr_name(c): name=c; break
                            if name: break
                        price=None
                        for pat in [r'"price"\s*:\s*"?([0-9]{4,7})"?\s*,\s*"priceCurrency"\s*:\s*"KRW"',r'"priceCurrency"\s*:\s*"KRW"\s*,\s*"price"\s*:\s*"?([0-9]{4,7})"?',r'"formattedValue"\s*:\s*"₩?\s*([0-9,]{4,10})"',r'([0-9]{1,3}(?:,[0-9]{3})+)\s*원']:
                            m=re.search(pat,dt,re.I)
                            if m:
                                price=_safe_price(m.group(1).replace(",",""))
                                if price is not None: break
                        if name or price is not None:
                            found[num]={"name_ko":name,"price":price,"currency":"KRW","source":"LEGO Korea 공식 카탈로그","source_url":d.url,"checked_at":time.strftime("%Y-%m-%d")}
                            if target: return found[num]
                    except Exception: continue
            except Exception: break
    return found.get(target) if target else found

def _official_kr_lookup(number):
    """v38.3: no Render-side LEGO.com crawling.
    Official Korean metadata comes only from verified repo/Supabase records.
    """
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

BLOCKED_PAGE_MARKERS = (
    "sorry, you have been blocked", "access denied", "attention required",
    "just a moment", "cloudflare", "captcha", "request blocked"
)

def _bad_page_text(value):
    x=(value or "").strip().lower()
    return (not x) or any(m in x for m in BLOCKED_PAGE_MARKERS)

def _clean_lego_title(s, number):
    if not s or _bad_page_text(str(s)): return None
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


def _valid_kr_name(name):
    return bool(name and re.search(r"[가-힣]",str(name)) and not _bad_page_text(str(name)))

@app.post("/api/kr-cleanup")
def kr_cleanup():
    """Known challenge/error text is ignored by all reads; this endpoint overwrites requested bad rows
    when a fresh Korean source can be found."""
    data=request.get_json(silent=True) or {}
    nums=[re.sub(r"[^0-9]","",str(x)) for x in (data.get("numbers") or [])][:50]
    fixed={}
    for n in [x for x in nums if x]:
        old=(sb_get([n]).get(n) if sb_enabled() else None) or {}
        if old and not _valid_kr_name(old.get("name_ko")):
            fb=_kr_catalog_fallback(n) or {}
            ins,ins_url=_instruction_name(n)
            name=fb.get("name_ko") or ins
            price=fb.get("price")
            if name or price is not None:
                row={"set_number":n,"name_ko":name,"price_krw":price,
                     "source":fb.get("source") or "LEGO Korea 조립 설명서",
                     "source_url":fb.get("source_url") or ins_url,
                     "checked_at":time.strftime("%Y-%m-%d")}
                sb_upsert([row]); fixed[n]=row
    return jsonify(ok=True,fixed=fixed,count=len(fixed))

@app.get("/api/kr-lookup/<number>")
def kr_lookup_debug(number):
    n=re.sub(r"[^0-9]","",str(number))
    if not n: return jsonify(ok=False,error="invalid set number"),400
    item,diag=_merge_kr_sources(n)
    if item and sb_enabled():
        sb_upsert([{"set_number":n,"name_ko":item.get("name_ko"),"price_krw":item.get("price"),
                    "source":item.get("source"),"source_url":item.get("source_url"),
                    "checked_at":item.get("checked_at")}])
    return jsonify(ok=bool(item),number=n,item=item,persistent=sb_enabled(),
                   diagnostics=diag)


@app.post("/api/kr-collect")
def kr_collect():
    data=request.get_json(silent=True) or {}
    nums=[re.sub(r"[^0-9]","",str(x)) for x in (data.get("numbers") or [])][:10]
    nums=[x for x in nums if x]
    results={}; diagnostics={}
    for n in nums:
        item,diag=_merge_kr_sources(n)
        diagnostics[n]=diag
        if item:
            results[n]=item
            if sb_enabled():
                sb_upsert([{"set_number":n,"name_ko":item.get("name_ko"),"price_krw":item.get("price"),
                            "source":item.get("source"),"source_url":item.get("source_url"),
                            "checked_at":item.get("checked_at")}])
    return jsonify(ok=True,items=results,count=len(results),diagnostics=diagnostics)

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

