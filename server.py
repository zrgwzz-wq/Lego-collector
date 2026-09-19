import os, time,json,time,requests,re
from flask import Flask,request,jsonify,send_from_directory
app=Flask(__name__); KEY=os.environ.get("BRICKSET_API_KEY","")
CACHE={}; TTL=21600
@app.get("/")
def home(): return send_from_directory(".","index.html")
@app.get("/api/health")
def health(): return jsonify(ok=True,key_configured=bool(KEY),cache_items=len(CACHE),kr_catalog_items=len(load_kr_catalog()) if "load_kr_catalog" in globals() else 0)
@app.get("/api/search")
def search():
    if not KEY:return jsonify(error="BRICKSET_API_KEY 미설정"),500
    q=request.args.get("q","").strip(); ck=q.lower()
    if ck in CACHE and time.time()-CACHE[ck]["t"]<TTL:
        out=dict(CACHE[ck]["d"]); out["cached"]=True; return jsonify(out)
    p={"apiKey":KEY,"userHash":"","params":json.dumps({"query":q,"pageSize":20,"extendedData":1})}
    r=requests.get("https://brickset.com/api/v3.asmx/getSets",params=p,timeout=20);r.raise_for_status()
    d=r.json(); CACHE[ck]={"t":time.time(),"d":d}; d["cached"]=False; return jsonify(d)
@app.get("/api/usage")
def usage():
    if not KEY:return jsonify(error="BRICKSET_API_KEY 미설정"),500
    r=requests.get("https://brickset.com/api/v3.asmx/getKeyUsageStats",params={"apiKey":KEY},timeout=20);r.raise_for_status()
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
