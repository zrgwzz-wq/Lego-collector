import os,json,time,requests,re
from flask import Flask,request,jsonify,send_from_directory
app=Flask(__name__); KEY=os.environ.get("BRICKSET_API_KEY","")
CACHE={}; TTL=21600
@app.get("/")
def home(): return send_from_directory(".","index.html")
@app.get("/api/health")
def health(): return jsonify(ok=True,key_configured=bool(KEY),cache_items=len(CACHE))
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


KR_CACHE={}; KR_TTL=86400
def clean_text(s):
    return re.sub(r'\s+',' ',re.sub(r'<[^>]+>',' ',s or '')).strip()

@app.get("/api/kr-meta")
def kr_meta():
    number=re.sub(r"[^0-9]","",request.args.get("number",""))
    if not number: return jsonify(found=False,error="invalid set number"),400
    if number in KR_CACHE and time.time()-KR_CACHE[number]["t"]<KR_TTL:
        d=dict(KR_CACHE[number]["d"]); d["cached"]=True; return jsonify(d)
    # Building-instructions page is much more stable for the official Korean set name.
    info_url=f"https://www.lego.com/ko-kr/service/building-instructions/{number}"
    search_url=f"https://www.lego.com/ko-kr/search?q={number}"
    headers={"User-Agent":"Mozilla/5.0","Accept-Language":"ko-KR,ko;q=0.9"}
    name=None; price=None
    try:
        ri=requests.get(info_url,headers=headers,timeout=15)
        if ri.ok:
            t=ri.text
            # Prefer og:title / h1 / title-like JSON fields.
            pats=[
              r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
              r'<h1[^>]*>(.*?)</h1>',
              r'"name"\s*:\s*"([^"]+)"'
            ]
            for pat in pats:
                m=re.search(pat,t,re.I|re.S)
                if m:
                    cand=clean_text(m.group(1))
                    cand=re.sub(r'\s*\|\s*LEGO.*$','',cand,flags=re.I)
                    if cand and number not in cand and len(cand)<120:
                        name=cand; break
    except: pass
    try:
        rs=requests.get(search_url,headers=headers,timeout=15)
        if rs.ok:
            t=rs.text
            positions=[m.start() for m in re.finditer(re.escape(number),t)]
            candidates=[]
            for pat in [r'([0-9]{1,3}(?:,[0-9]{3})+)\s*원',r'₩\s*([0-9]{1,3}(?:,[0-9]{3})+)']:
                for m in re.finditer(pat,t):
                    try: val=int(m.group(1).replace(",",""))
                    except: continue
                    if 1000<=val<=10000000 and positions:
                        dist=min(abs(m.start()-p) for p in positions)
                        if dist<12000: candidates.append((dist,val))
            if candidates: price=sorted(candidates)[0][1]
    except: pass
    d={"found":bool(name or price),"number":number,"name_ko":name,"price":price,
       "currency":"KRW" if price else None,"source":"LEGO Korea",
       "name_source_url":info_url if name else None,
       "price_source_url":search_url if price else None,
       "checked_at":time.strftime("%Y-%m-%d")}
    KR_CACHE[number]={"t":time.time(),"d":d}
    return jsonify(d)

# Backward-compatible endpoint used by older client code.
@app.get("/api/kr-price")
def kr_price_compat():
    number=request.args.get("number","")
    with app.test_request_context("/api/kr-meta?number="+number):
        return kr_meta()
