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
@app.get("/api/kr-price")
def kr_price():
    number=re.sub(r"[^0-9]","",request.args.get("number",""))
    if not number: return jsonify(found=False,error="invalid set number"),400
    if number in KR_CACHE and time.time()-KR_CACHE[number]["t"]<KR_TTL:
        d=dict(KR_CACHE[number]["d"]); d["cached"]=True; return jsonify(d)
    url="https://www.lego.com/ko-kr/search?q="+number
    try:
        rr=requests.get(url,headers={"User-Agent":"Mozilla/5.0","Accept-Language":"ko-KR,ko;q=0.9"},timeout=15)
        rr.raise_for_status()
        text=rr.text
        # Only accept a KRW price when the set number is present close to the price in LEGO's own response.
        candidates=[]
        patterns=[r'([0-9]{1,3}(?:,[0-9]{3})+)\\s*원',
                  r'₩\\s*([0-9]{1,3}(?:,[0-9]{3})+)',
                  r'"price"\\s*:\\s*"?([0-9]{4,9}(?:\\.[0-9]+)?)"?']
        positions=[m.start() for m in re.finditer(re.escape(number),text)]
        for pat in patterns:
            for m in re.finditer(pat,text):
                raw=m.group(1)
                try: val=int(float(raw.replace(",","")))
                except: continue
                if val < 1000 or val > 10000000: continue
                if any(abs(m.start()-p)<12000 for p in positions):
                    candidates.append((min(abs(m.start()-p) for p in positions),val))
        price=sorted(candidates)[0][1] if candidates else None
        d={"found":bool(price),"number":number,"price":price,"currency":"KRW",
           "source":"LEGO Korea","source_url":url if price else None,
           "checked_at":time.strftime("%Y-%m-%d")}
        KR_CACHE[number]={"t":time.time(),"d":d}
        return jsonify(d)
    except Exception as e:
        return jsonify(found=False,number=number,error="LEGO Korea 조회 실패"),502
