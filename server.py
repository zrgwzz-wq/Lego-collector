import os, time,json,time,requests,re,html as html_lib,threading,math,urllib.parse,hmac,hashlib,base64,secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from flask import Flask,request,jsonify,send_from_directory
app=Flask(__name__); KEY=os.environ.get("BRICKSET_API_KEY","")
API="https://brickset.com/api/v3.asmx"
CACHE={}; TTL=21600

@app.get("/api/kr-catalog-stats")
def api_kr_catalog_stats():
    local_count=len(load_kr_catalog())
    remote_count=0
    try:
        base=(os.getenv("SUPABASE_URL") or "").rstrip("/")
        key=os.getenv("SUPABASE_SERVICE_KEY") or ""
        if base and key:
            headers={"apikey":key,"Authorization":f"Bearer {key}","Prefer":"count=exact"}
            r=requests.get(base+"/rest/v1/lego_kr_catalog",params={"select":"set_number","limit":"1"},headers=headers,timeout=8)
            cr=r.headers.get("Content-Range","")
            tail=cr.rsplit("/",1)[-1] if "/" in cr else ""
            if tail.isdigit(): remote_count=int(tail)
    except Exception:
        pass
    return jsonify(ok=True,local_count=local_count,remote_count=remote_count,
                   effective_count=max(local_count,remote_count),
                   source=("Supabase" if remote_count else "local fallback"))

@app.get("/api/kr-overlay/<number>")
def api_kr_overlay(number):
    n=re.sub(r"[^0-9]","",str(number))
    if not n:
        return jsonify(ok=False,error="invalid set number"),400

    # v40: use exactly the same proven pipeline as /api/kr-lookup.
    item,diag=_merge_kr_sources(n)
    if not item:
        return jsonify(ok=False,number=n,name_ko=None,price=None,currency="KRW",
                       name_source=None,price_source=None,
                       diagnostics=diag,validation="brickset-ko-overlay-v87")

    name=item.get("name_ko")
    price=item.get("price")
    source=item.get("source")
    # Current DB schema has one source field; expose it conservatively.
    name_source=item.get("name_source") if name else None
    price_source=item.get("price_source") if price is not None else None
    price_type=item.get("price_type") if price is not None else None

    if sb_enabled():
        sb_upsert([{"set_number":n,"name_ko":name,"price_krw":price,
                    "source":source,"source_url":item.get("source_url"),
                    "checked_at":item.get("checked_at")}])

    return jsonify(ok=True,number=n,name_ko=name,price=price,
                   currency=item.get("currency") or "KRW",
                   name_source=name_source,price_source=price_source,price_type=price_type,
                   diagnostics=diag,validation="brickset-ko-overlay-v87")


@app.post("/api/kr-catalog-import")
def api_kr_catalog_import():
    """Bulk import verified Korean metadata into Supabase.

    JSON body:
      {"items":[
        {"number":"10342","name_ko":"핑크 꽃다발","price":79900,
         "source":"LEGO Korea 공식","source_url":"..."}
      ]}

    Safe rules:
    - max 1000 rows per request
    - LEGO Korea official rows get official_msrp provenance
    - other verified prices get release_price provenance
    - name and price provenance are stored independently
    """
    body=request.get_json(silent=True) or {}
    items=body.get("items") or body.get("rows") or []
    if not isinstance(items,list):
        return jsonify(ok=False,error="items must be a list"),400
    if len(items)>1000:
        return jsonify(ok=False,error="maximum 1000 rows per import"),400

    imported=[]; failed=[]
    for raw in items:
        try:
            n=str(raw.get("number") or raw.get("set_number") or "").strip().split("-")[0]
            if not n or not n.isdigit():
                raise ValueError("invalid set number")
            name=(raw.get("name_ko") or "").strip() or None
            price=raw.get("price")
            if price in ("",None):
                price=None
            else:
                price=int(str(price).replace(",",""))
                if price<=0: price=None
            source=(raw.get("source") or "검증된 한국 카탈로그").strip()
            source_url=(raw.get("source_url") or "").strip() or None
            checked=(raw.get("checked_at") or time.strftime("%Y-%m-%d",time.gmtime()))

            # Persist through the existing catalog table helper.
            ok=sb_put({
                "set_number":n,
                "name_ko":name,
                "price_krw":price,
                "source":source,
                "source_url":source_url,
                "checked_at":checked
            })
            if not ok:
                raise RuntimeError("Supabase catalog write failed")

            is_official=("LEGO Korea" in source and "조립설명서" not in source and
                         "instructions" not in source.lower())
            name_source=("LEGO Korea" if is_official else source) if name else None
            price_source=("LEGO Korea" if is_official else source) if price is not None else None
            price_type=("official_msrp" if is_official else "release_price") if price is not None else None
            sb_provenance_put(n,name_source,price_source,price_type)
            imported.append({"number":n,"name_ko":name,"price":price,
                             "name_source":name_source,"price_source":price_source,
                             "price_type":price_type})
        except Exception as e:
            failed.append({"number":str(raw.get("number") or raw.get("set_number") or ""),
                           "error":str(e)})
    return jsonify(ok=(len(failed)==0),imported=len(imported),failed=len(failed),
                   results=imported,errors=failed,validation="bulk-catalog-v87")

@app.post("/api/kr-name-sync")
def api_kr_name_sync():
    """Resolve official Korean names from LEGO Korea building-instructions pages.
    Successful names are cached in Supabase. Existing prices are preserved.
    """
    body=request.get_json(silent=True) or {}
    numbers=body.get("numbers") or []
    if isinstance(numbers,str):
        numbers=[x.strip() for x in numbers.split(",") if x.strip()]
    if not isinstance(numbers,list) or not numbers:
        return jsonify(ok=False,error="numbers required"),400

    clean=[]
    for raw in numbers[:100]:
        n=re.sub(r"[^0-9]","",str(raw))
        if n and n not in clean: clean.append(n)
    existing=sb_get(clean) if sb_enabled() else {}
    results=[]; saved=0
    for n in clean:
        try:
            name,url=_instruction_name(n)
        except Exception:
            name,url=None,None
        if name and _valid_kr_name(name):
            old=existing.get(n) or {}
            old_price=old.get("price_krw")
            old_source=(old.get("source") or "").strip()
            source="LEGO Korea 조립설명서"
            if old_price is not None and old_source and "LEGO Korea" not in old_source:
                source=f"LEGO Korea 조립설명서 + {old_source} 가격"
            row={"set_number":n,"name_ko":name,"price_krw":old_price,
                 "source":source,"source_url":url or old.get("source_url"),
                 "checked_at":time.strftime("%Y-%m-%d")}
            did_save=bool(sb_enabled() and sb_upsert([row]))
            saved += 1 if did_save else 0
            results.append({"number":n,"name_ko":name,"saved":did_save,"source_url":url})
        else:
            results.append({"number":n,"name_ko":None,"saved":False})
    return jsonify(ok=True,requested=len(clean),saved=saved,results=results,
                   validation="lego-instruction-name-sync-v41")


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
        version="v87"
    )
@app.get("/api/search")
def search():
    if not KEY:return jsonify(error="BRICKSET_API_KEY 미설정"),500
    q=request.args.get("q","").strip(); ck=q.lower()
    if ck in CACHE and time.time()-CACHE[ck]["t"]<TTL:
        out=dict(CACHE[ck]["d"]); out["cached"]=True; return jsonify(out)

    # Brickset's default ordering is by set number, not search relevance.
    # For text/product-name searches ask Brickset for ranked results and a
    # larger candidate pool, then keep only genuine name matches below.
    numeric_query=bool(re.fullmatch(r"\d+(?:-\d+)?",q))
    params={"query":q,"pageSize":20 if numeric_query else 100,"extendedData":1}
    if not numeric_query:
        params["orderBy"]="Rank"
    p={"apiKey":KEY,"userHash":"","params":json.dumps(params)}
    r=requests.get(API+"/getSets",params=p,timeout=20);r.raise_for_status()
    d=r.json()

    if not numeric_query:
        def norm_text(v):
            return re.sub(r"[^a-z0-9]+"," ",str(v or "").lower()).strip()

        nq=norm_text(q)
        tokens=[t for t in nq.split() if t]
        ranked=[]
        for item in d.get("sets",[]) or []:
            nn=norm_text(item.get("name"))
            if not nn:
                continue

            score=0
            if nn==nq:
                score=1000
            elif nn.startswith(nq+" ") or nn.startswith(nq):
                score=900
            elif nq and nq in nn:
                score=800
            elif tokens and all(t in nn.split() or t in nn for t in tokens):
                score=700

            # Global search is explicitly "set number or product name".
            # Theme/subtheme-only matches are deliberately excluded so a query
            # like "Dune" cannot show unrelated Space/Technic sets.
            if score:
                item["_name_match_score"]=score
                ranked.append(item)

        ranked.sort(key=lambda x:(-int(x.get("_name_match_score",0)),
                                  int(x.get("year") or 0)*-1,
                                  str(x.get("number") or "")))
        for item in ranked:
            item.pop("_name_match_score",None)
        d["sets"]=ranked[:20]
        d["matches"]=len(ranked)
        d["search_mode"]="product_name"

    # v87: when the user enters an exact set number, keep that set as the
    # primary result and classify only evidence-backed extra hits as relations.
    exact_num=re.sub(r"[^0-9]","",q)
    if exact_num and numeric_query:
        for item in d.get("sets",[]):
            n=str(item.get("number") or "").split("-")[0]
            if n==exact_num:
                item["relation_type"]="primary"
                continue
            ext=item.get("extendedData") or {}
            notes=str(ext.get("notes") or item.get("notes") or "")
            raw_tags=ext.get("tags") if ext.get("tags") is not None else item.get("tags")
            tags=" ".join(raw_tags or []) if isinstance(raw_tags,list) else str(raw_tags or "")
            hay=(notes+" "+tags+" "+str(item.get("theme") or "")+" "+str(item.get("subtheme") or "")).lower()
            rel=None
            # Require an explicit reference to the searched set for strong relationships.
            mentions=exact_num in hay
            if mentions and ("gift with purchase" in hay or "gwp" in hay or "free with qualifying purchases" in hay): rel="gwp"
            elif mentions and ("contains" in hay or "bundle" in hay or "multipack" in hay or "multi-pack" in hay): rel="bundle"
            elif mentions and ("connects with" in hay or "expansion" in hay or "extension" in hay): rel="connects"
            elif mentions and ("redesign" in hay or "revision" in hay or "revised" in hay): rel="revision"
            elif mentions and ("remake" in hay or "re-release" in hay or "similar set" in hay): rel="remake"
            if rel:
                item["relation_type"]=rel
                try:
                    relation_put(exact_num,n,rel,"Brickset",notes[:500])
                    relation_put(n,exact_num,"parent_"+rel,"Brickset",notes[:500])
                except Exception:
                    pass
        # Merge persisted relations. This makes reverse navigation work even when
        # Brickset does not return the parent set in a query for the GWP itself.
        if sb_enabled():
            try:
                ru=f"{SUPABASE_URL}/rest/v1/{RELATION_TABLE}"
                rr=requests.get(ru,headers=_sb_headers(),
                    params={"or":f"(set_number.eq.{exact_num},related_set_number.eq.{exact_num})","select":"*"},
                    timeout=6)
                if rr.ok:
                    known={str(x.get("number") or "").split("-")[0] for x in d.get("sets",[])}
                    for edge in rr.json() or []:
                        if str(edge.get("set_number"))==exact_num:
                            other=str(edge.get("related_set_number") or "")
                            rtype=str(edge.get("relation_type") or "")
                        else:
                            other=str(edge.get("set_number") or "")
                            base=str(edge.get("relation_type") or "")
                            rtype=base if base.startswith("parent_") else "parent_"+base
                        if not other or other in known: continue
                        bp={"apiKey":KEY,"userHash":"","params":json.dumps({"query":other,"pageSize":5,"extendedData":1})}
                        br=requests.get(API+"/getSets",params=bp,timeout=12)
                        if br.ok:
                            for bx in br.json().get("sets",[]):
                                if str(bx.get("number") or "").split("-")[0]==other:
                                    bx["relation_type"]=rtype
                                    d.setdefault("sets",[]).append(bx)
                                    known.add(other)
                                    break
            except Exception:
                pass
        d["primary_number"]=exact_num
    CACHE[ck]={"t":time.time(),"d":d}; d["cached"]=False; return jsonify(d)
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
    if data:
        return jsonify(found=True,number=number,**data)
    stored=(sb_get([number]).get(number) if number and sb_enabled() else None) or {}
    if stored:
        return jsonify(found=True,number=number,
                       name_ko=stored.get("name_ko"),price=stored.get("price_krw"),
                       currency="KRW",source=stored.get("source"),
                       source_url=stored.get("source_url"),checked_at=stored.get("checked_at"))
    return jsonify(found=False,number=number)


def _prov_headers():
    return {**_sb_headers(),"Prefer":"return=representation"}

def sb_provenance_get(number):
    if not sb_enabled(): return {}
    n=str(number).strip().split("-")[0]
    try:
        u=f"{SUPABASE_URL}/rest/v1/lego_kr_provenance?set_number=eq.{n}&select=*"
        r=requests.get(u,headers=_sb_headers(),timeout=8)
        if r.ok and r.json(): return r.json()[0]
    except Exception:
        pass
    return {}

def sb_provenance_put(number,name_source=None,price_source=None,price_type=None):
    if not sb_enabled(): return False
    n=str(number).strip().split("-")[0]
    payload={"set_number":n,"name_source":name_source,"price_source":price_source,
             "price_type":price_type,"updated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        u=f"{SUPABASE_URL}/rest/v1/lego_kr_provenance?on_conflict=set_number"
        r=requests.post(u,headers={**_prov_headers(),"Prefer":"resolution=merge-duplicates,return=minimal"},
                        json=payload,timeout=8)
        return r.ok
    except Exception:
        return False


SEARCH_ALIAS_TABLE="lego_search_aliases"
SEARCH_ALIAS_CACHE={}
KR_QUERY_CACHE={}
KR_QUERY_TTL=21600
DOMESTIC_NAME_CACHE={}
DOMESTIC_NAME_TTL=21600

def _alias_norm(value):
    return re.sub(r"\s+"," ",str(value or "").strip().lower())

def search_alias_get(query):
    nq=_alias_norm(query)
    if not nq: return []
    rows=[]; seen=set()
    for row in SEARCH_ALIAS_CACHE.values():
        if nq in str(row.get("alias_normalized") or ""):
            n=str(row.get("set_number") or "")
            if n and n not in seen:
                rows.append(dict(row)); seen.add(n)
    if sb_enabled():
        try:
            r=requests.get(
                f"{SUPABASE_URL}/rest/v1/{SEARCH_ALIAS_TABLE}",
                headers=_sb_headers(),
                params={"select":"alias,set_number,alias_type,source,source_url",
                        "alias_normalized":f"ilike.*{nq}*","limit":20},
                timeout=2)
            if r.ok:
                for row in r.json() or []:
                    n=str(row.get("set_number") or "")
                    if n and n not in seen:
                        rows.append(row); seen.add(n)
        except Exception:
            pass
    return rows[:20]

def search_alias_put(alias, number, alias_type="korean_query", source="자동 검색 연결", source_url=None):
    a=str(alias or "").strip()
    n=re.sub(r"[^0-9]","",str(number or ""))
    na=_alias_norm(a)
    if not a or not na or not n: return False
    row={"alias":a,"alias_normalized":na,"set_number":n,
         "alias_type":alias_type,"source":source,"source_url":source_url,
         "updated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
    SEARCH_ALIAS_CACHE[(na,n)]=row
    if not sb_enabled(): return True
    try:
        r=requests.post(
            f"{SUPABASE_URL}/rest/v1/{SEARCH_ALIAS_TABLE}",
            headers=_sb_headers("resolution=merge-duplicates,return=minimal"),
            params={"on_conflict":"alias_normalized,set_number"},
            json=row,timeout=7)
        return r.ok
    except Exception:
        return False

def _brickset_exact_set(number):
    n=re.sub(r"[^0-9]","",str(number or ""))
    if not n or not KEY: return None
    try:
        params=json.dumps({"setNumber":n+"-1","pageSize":1,"extendedData":1})
        r=requests.get(API+"/getSets",
                       params={"apiKey":KEY,"userHash":"","params":params},
                       timeout=9)
        if not r.ok: return None
        for item in r.json().get("sets",[]) or []:
            if str(item.get("number") or "").split("-")[0]==n:
                return item
    except Exception:
        pass
    return None

def _query_candidate_numbers(page_html, query):
    """Discovery only. Search-page text is never persisted as a product name."""
    if not page_html: return []
    text=html_lib.unescape(_decode_jsonish(page_html))
    text=re.sub(r"<script[\s\S]*?</script>"," ",text,flags=re.I)
    text=re.sub(r"<style[\s\S]*?</style>"," ",text,flags=re.I)
    plain=re.sub(r"<[^>]+>"," ",text)
    plain=re.sub(r"\s+"," ",plain)
    q=re.sub(r"\s+","",str(query or "").lower())
    if not q: return []
    out=[]
    for m in re.finditer(r"(?<!\d)(\d{3,7})(?:-\d+)?(?!\d)",plain):
        n=m.group(1)
        try:
            iv=int(n)
            if 1900<=iv<=2100: continue
        except Exception:
            pass
        area=plain[max(0,m.start()-260):min(len(plain),m.end()+260)]
        compact=re.sub(r"\s+","",area.lower())
        if q not in compact: continue
        if "레고" not in area and "lego" not in area.lower(): continue
        if n not in out: out.append(n)
        if len(out)>=12: break
    return out

def _persist_discovered_name(number, name, source, source_url=None):
    n=re.sub(r"[^0-9]","",str(number or ""))
    safe=_safe_kr_product_name(name,n)
    if not n or not safe: return False
    if not sb_enabled(): return True
    old=(sb_get([n]).get(n) or {})
    prov=sb_provenance_get(n)
    row={"set_number":n,"name_ko":safe,
         "price_krw":old.get("price_krw"),
         "source":old.get("source") or source,
         "source_url":old.get("source_url") or source_url,
         "checked_at":time.strftime("%Y-%m-%d")}
    ok=sb_upsert([row])
    sb_provenance_put(n,source,prov.get("price_source"),prov.get("price_type"))
    return bool(ok)

def _domestic_kr_name_lookup(number):
    """Find a Korean name only from an exact-number domestic product detail page."""
    n=re.sub(r"[^0-9]","",str(number or ""))
    if not n: return None
    cached=DOMESTIC_NAME_CACHE.get(n)
    if cached and time.time()-cached["t"]<DOMESTIC_NAME_TTL:
        return cached["v"]

    found=None
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs=[ex.submit(_danawa_kr_lookup,n),ex.submit(_kream_kr_lookup,n)]
            for fut in as_completed(futs):
                try: row=fut.result()
                except Exception: row=None
                if row and row.get("name_ko"):
                    found={"name_ko":row.get("name_ko"),
                           "source":row.get("name_source") or row.get("source") or "국내 표기",
                           "source_url":row.get("source_url")}
                    break
    except Exception:
        pass
    DOMESTIC_NAME_CACHE[n]={"t":time.time(),"v":found}
    if found:
        _persist_discovered_name(n,found["name_ko"],found["source"],found.get("source_url"))
    return found

def _discover_korean_query(query):
    """Korean query -> domestic discovery -> exact Brickset validation -> learned alias."""
    q=str(query or "").strip()
    if not q or not re.search(r"[가-힣]",q): return []
    ck=_alias_norm(q)
    hit=KR_QUERY_CACHE.get(ck)
    if hit and time.time()-hit["t"]<KR_QUERY_TTL:
        return hit["v"]

    headers={"User-Agent":"Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/143 Mobile Safari/537.36",
             "Accept-Language":"ko-KR,ko;q=0.9,en;q=0.7"}
    providers=[
        ("다나와","https://search.danawa.com/mobile/dsearch.php",{"keyword":q}),
        ("KREAM","https://kream.co.kr/search",{"keyword":q})
    ]
    candidates={}

    def fetch_provider(provider):
        name,url,params=provider
        try:
            r=requests.get(url,params=params,headers=headers,timeout=8)
            if r.ok and not _bad_page_text((r.text or "")[:5000]):
                return name,r.url,_query_candidate_numbers(r.text,q)
        except Exception:
            pass
        return name,url,[]

    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs=[ex.submit(fetch_provider,p) for p in providers]
            for fut in as_completed(futs):
                name,url,nums=fut.result()
                for n in nums:
                    candidates.setdefault(n,{"source":name+" 검색","source_url":url})
    except Exception:
        pass

    rows=[]
    nums=list(candidates)[:12]
    if nums:
        try:
            with ThreadPoolExecutor(max_workers=min(4,len(nums))) as ex:
                futs={ex.submit(_brickset_exact_set,n):n for n in nums}
                for fut in as_completed(futs):
                    n=futs[fut]
                    try: item=fut.result()
                    except Exception: item=None
                    if not item: continue
                    domestic=_domestic_kr_name_lookup(n) or {}
                    rows.append({"number":n,
                                 "name_ko":domestic.get("name_ko"),
                                 "name_en":item.get("name"),
                                 "source":domestic.get("source") or candidates[n]["source"],
                                 "discovery_source":candidates[n]["source"]})
                    search_alias_put(q,n,"korean_query",candidates[n]["source"],
                                     candidates[n].get("source_url"))
        except Exception:
            pass

    rows.sort(key=lambda x:(0 if x.get("name_ko") and ck in _alias_norm(x.get("name_ko")) else 1,
                            str(x.get("number") or "")))
    KR_QUERY_CACHE[ck]={"t":time.time(),"v":rows[:20]}
    return rows[:20]


KO_TRANSLATE_CACHE={}
KO_FAST_TERM_HINTS={
    # Common LEGO franchise/product words. These are only query bridges.
    "생텀":"sanctum", "생토럼":"sanctorum",
    "어벤져스":"avengers", "아이언맨":"iron man", "스파이더맨":"spider man",
    "배트맨":"batman", "슈퍼맨":"superman", "원더우먼":"wonder woman",
    "캡틴 아메리카":"captain america", "헐크":"hulk", "토르":"thor",
    "엑스맨":"x men", "마블":"marvel", "스타워즈":"star wars",
    "해리포터":"harry potter", "반지의 제왕":"lord of the rings",
    "리븐델":"rivendell", "바라드두르":"barad dur", "듄":"dune",
    "쥬라기":"jurassic", "디즈니":"disney", "미키":"mickey", "미니":"minnie",
    "마인크래프트":"minecraft", "닌자고":"ninjago", "소닉":"sonic",
    "트랜스포머":"transformers", "옵티머스 프라임":"optimus prime",
    "백 투 더 퓨처":"back to the future", "타이타닉":"titanic",
    "에펠탑":"eiffel tower", "콩코드":"concorde", "포르쉐":"porsche",
    "페라리":"ferrari", "람보르기니":"lamborghini", "메르세데스":"mercedes",
    "포뮬러 1":"formula 1", "포뮬러원":"formula 1", "에프원":"f1",
    "테크닉":"technic", "아이콘":"icons", "아이디어":"ideas",
    "아키텍처":"architecture", "크리에이터":"creator", "시티":"city",
    "프렌즈":"friends", "캐슬":"castle", "해적":"pirate", "우주":"space",
    "기차":"train", "자동차":"car", "타워":"tower", "성":"castle"
}

def _translate_ko_to_en(query):
    """Best-effort Korean -> English fallback for product-name discovery.
    Uses Google's public translate endpoint only as a query bridge; the translated
    text is never stored as product metadata.
    """
    q=str(query or "").strip()
    if not q: return None
    ck=_alias_norm(q)
    if ck in KO_TRANSLATE_CACHE:
        return KO_TRANSLATE_CACHE[ck]
    if ck in KO_FAST_TERM_HINTS:
        translated=KO_FAST_TERM_HINTS[ck]
        KO_TRANSLATE_CACHE[ck]=translated
        return translated
    # Phrase substitution handles mixed queries such as "어벤져스 타워".
    hinted=ck
    changed=False
    for ko,en in sorted(KO_FAST_TERM_HINTS.items(),key=lambda kv:len(kv[0]),reverse=True):
        if ko in hinted:
            hinted=hinted.replace(ko," "+en+" ")
            changed=True
    if changed:
        hinted=re.sub(r"\s+"," ",hinted).strip()
        if hinted and re.search(r"[A-Za-z]",hinted) and not re.search(r"[가-힣]",hinted):
            KO_TRANSLATE_CACHE[ck]=hinted
            return hinted
    translated=None
    try:
        r=requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client":"gtx","sl":"ko","tl":"en","dt":"t","q":q},
            headers={"User-Agent":"Mozilla/5.0"},
            timeout=1.5)
        if r.ok:
            data=r.json()
            parts=[]
            for row in (data[0] or []):
                if isinstance(row,list) and row and row[0]:
                    parts.append(str(row[0]))
            text=" ".join(parts).strip()
            # Require a meaningful ASCII translation, not unchanged Hangul.
            if text and re.search(r"[A-Za-z]",text) and not re.search(r"[가-힣]",text):
                translated=text
    except Exception:
        translated=None
    KO_TRANSLATE_CACHE[ck]=translated
    return translated

def _brickset_name_candidates(query, limit=12):
    """Search Brickset by product name and keep genuine name matches only."""
    q=str(query or "").strip()
    if not q or not KEY: return []
    try:
        params={"query":q,"pageSize":100,"extendedData":1,"orderBy":"Rank"}
        r=requests.get(API+"/getSets",
                       params={"apiKey":KEY,"userHash":"","params":json.dumps(params)},
                       timeout=3.5)
        if not r.ok: return []
        items=r.json().get("sets",[]) or []
    except Exception:
        return []

    def norm(v):
        return re.sub(r"[^a-z0-9]+"," ",str(v or "").lower()).strip()

    nq=norm(q)
    toks=[t for t in nq.split() if t]
    ranked=[]
    for item in items:
        nn=norm(item.get("name"))
        if not nn: continue
        score=0
        if nn==nq: score=1000
        elif nn.startswith(nq+" ") or nn.startswith(nq): score=920
        elif nq and nq in nn: score=850
        elif toks and all(t in nn for t in toks): score=760
        if score:
            ranked.append((score,item))
    ranked.sort(key=lambda x:(-x[0], -(int(x[1].get("year") or 0))))
    return [x[1] for x in ranked[:limit]]

def _translated_korean_fallback(query):
    """Fast Korean -> English bridge. No per-candidate domestic web calls here."""
    q=str(query or "").strip()
    en=_translate_ko_to_en(q)
    if not en: return {"translated_query":None,"results":[]}
    candidates=_brickset_name_candidates(en,12)
    rows=[]
    local=load_kr_catalog()
    for item in candidates:
        n=str(item.get("number") or "").split("-")[0]
        if not n: continue
        saved=(local.get(n) or {})
        rows.append({
            "number":n,"name_ko":saved.get("name_ko"),"name_en":item.get("name"),
            "source":saved.get("source") or "영문명 자동 연결",
            "match":"translated_name","translated_query":en,
            "year":item.get("year"),"theme":item.get("theme")})
    if len(rows)==1:
        search_alias_put(q,rows[0]["number"],"ko_to_en_product_name",f"자동 영문 연결: {en}",None)
    return {"translated_query":en,"results":rows[:20]}

@app.get("/api/kr-name-search")
def api_kr_name_search():
    q=(request.args.get("q") or "").strip()
    ql=q.lower()
    if not q:
        return jsonify(ok=True,results=[],validation="smart-ko-name-search-v87")

    rows=[]; seen=set(); strategy=[]
    is_korean=bool(re.search(r"[가-힣]",q))

    # 1) Local verified Korean catalog.
    for n,row in load_kr_catalog().items():
        name=str((row or {}).get("name_ko") or "")
        if ql in name.lower() or ql in str(n).lower():
            rows.append({
                "number":str(n),"name_ko":name,"name_en":None,
                "source":(row or {}).get("source"),"match":"catalog"
            })
            seen.add(str(n))
    if rows:
        strategy.append("local_catalog")

    # 2) Supabase Korean catalog + learned aliases. Short, bounded lookups.
    remote_rows=[]; alias_rows=[]
    if sb_enabled():
        def remote_catalog():
            try:
                url=f"{SUPABASE_URL}/rest/v1/lego_kr_catalog"
                r=requests.get(url,headers=_sb_headers(),
                               params={"select":"set_number,name_ko,source",
                                       "name_ko":f"ilike.*{q}*","limit":20},
                               timeout=2)
                return r.json() if r.ok else []
            except Exception:
                return []
        def remote_alias():
            try:
                return search_alias_get(q)
            except Exception:
                return []
        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                f1=ex.submit(remote_catalog)
                f2=ex.submit(remote_alias)
                remote_rows=f1.result()
                alias_rows=f2.result()
        except Exception:
            remote_rows=[]; alias_rows=[]

    for row in remote_rows or []:
        n=str(row.get("set_number") or "")
        if n and n not in seen:
            rows.append({"number":n,"name_ko":row.get("name_ko"),
                         "name_en":None,"source":row.get("source"),
                         "match":"catalog"})
            seen.add(n)

    for a in alias_rows or []:
        n=str(a.get("set_number") or "")
        if n and n not in seen:
            rows.append({"number":n,"name_ko":None,"name_en":None,
                         "source":a.get("source"),"match":"alias"})
            seen.add(n)

    if remote_rows: strategy.append("supabase_catalog")
    if alias_rows: strategy.append("alias")

    # 3) Full master-catalog Korean/English aliases already stored in Supabase.
    try:
        for m in master_name_search(q,20):
            n=str(m.get("set_number") or "")
            if n and n not in seen:
                rows.append({"number":n,"name_ko":m.get("name_ko"),
                             "name_en":m.get("name_en"),"source":"마스터 카탈로그",
                             "match":"master_catalog","year":m.get("year"),
                             "theme":m.get("theme")})
                seen.add(n)
        if any(x.get("match")=="master_catalog" for x in rows):
            strategy.append("master_catalog")
    except Exception:
        pass

    # 4) Korean query -> English product-name expansion.
    # IMPORTANT: run this even when one Korean catalog match already exists,
    # because there may be other LEGO sets with the same word in their English name.
    translated_query=None
    if is_korean:
        translated_query=_translate_ko_to_en(q)
        if translated_query:
            for item in _brickset_name_candidates(translated_query,20):
                n=str(item.get("number") or "").split("-")[0]
                if not n or n in seen:
                    # Fill missing English title on an existing Korean result.
                    for row in rows:
                        if str(row.get("number"))==n and not row.get("name_en"):
                            row["name_en"]=item.get("name")
                    continue
                rows.append({
                    "number":n,
                    "name_ko":None,
                    "name_en":item.get("name"),
                    "source":f"영문 제품명 연결: {translated_query}",
                    "match":"translated_name",
                    "year":item.get("year"),
                    "theme":item.get("theme")
                })
                seen.add(n)
            if any(x.get("match")=="translated_name" for x in rows):
                strategy.append("translated_name")

    # Prefer Korean exact/contains matches, then English translated-name matches,
    # then newest sets. No slow KREAM/Danawa crawling here.
    nq=_alias_norm(q)
    rows.sort(key=lambda x:(
        0 if x.get("name_ko") and nq in _alias_norm(x.get("name_ko")) else 1,
        0 if x.get("match")=="translated_name" else 1,
        -(int(x.get("year") or 0)),
        str(x.get("number") or "")
    ))

    return jsonify(ok=True,results=rows[:20],
                   translated_query=translated_query,
                   strategy=strategy,
                   smart_search=is_korean,
                   validation="smart-ko-name-search-v87")
@app.get("/api/kr-fast/<number>")
def api_kr_fast(number):
    n=str(number).strip().split("-")[0]
    if not n:
        return jsonify(ok=False,error="invalid set number"),400
    fast_only=(request.args.get("fast")=="1")

    verified=(load_kr_catalog().get(n) or {})
    stored=(sb_get([n]).get(n) if sb_enabled() else None) or {}
    provenance=sb_provenance_get(n)

    name=verified.get("name_ko") or stored.get("name_ko")
    price=verified.get("price")
    if price is None:
        price=stored.get("price_krw")

    src_verified=verified.get("source") or ""
    src_stored=stored.get("source") or ""

    # Name and price provenance MUST be independent.
    name_source=(src_verified if verified.get("name_ko") else src_stored if stored.get("name_ko") else None)

    def normalize_price_source(src):
        text=str(src or "")
        # Old cached rows may contain a combined source such as
        # "LEGO Korea 조립설명서 + KREAM 가격". The price came from KREAM.
        if "KREAM" in text.upper():
            return "KREAM"
        if "LEGO Korea 공식" in text or ("LEGO Korea" in text and "조립설명서" not in text):
            return "LEGO Korea"
        if "국내" in text or "한국 카탈로그" in text or "BrickMecha" in text:
            return "국내 판매자료"
        return text or None

    raw_price_source=(src_verified if verified.get("price") is not None
                      else src_stored if stored.get("price_krw") is not None else None)

    # v87 precedence repair:
    # A trusted repo/official catalog price is newer authority than stale Supabase provenance.
    # Never allow an old KREAM provenance row to relabel a verified official price.
    if verified.get("price") is not None:
        price_source=normalize_price_source(src_verified)
        name_source=(src_verified if verified.get("name_ko") else
                     provenance.get("name_source") or name_source)
    else:
        price_source=provenance.get("price_source") or normalize_price_source(raw_price_source)
        name_source=provenance.get("name_source") or name_source

    # Migration repair for legacy mixed rows: if a KR price exists but its cached source
    # is only the LEGO building-instructions name source, it is NOT proof of LEGO MSRP.
    if price is not None and "조립설명서" in str(raw_price_source or "") and not provenance.get("price_source"):
        price_source=None

    def ptype(src):
        return "official_msrp" if src=="LEGO Korea" else "release_price"

    # v87: a cached price does not imply that the Korean product name is known.
    # Fill a missing name from strict exact-number domestic detail pages.
    if not name and not fast_only:
        domestic_name=_domestic_kr_name_lookup(n)
        if domestic_name and domestic_name.get("name_ko"):
            name=domestic_name.get("name_ko")
            name_source=domestic_name.get("source") or "국내 표기"

    # v87 fast-only mode: return cached/local data immediately and never crawl.
    if fast_only:
        resolved_type=(ptype(price_source) if price is not None and price_source else None)
        return jsonify(ok=True,number=n,name_ko=name,price=price,currency="KRW",
                       name_source=name_source,price_source=price_source,
                       price_type=resolved_type,cache_hit=bool(name or price is not None),
                       fast_only=True,validation="cache-first-v87")

    # Fast path: cached/verified KR price already exists.
    if price is not None and price_source is not None:
        # Self-heal stale provenance so subsequent requests stay correct.
        resolved_type=ptype(price_source)
        if verified.get("price") is not None:
            sb_provenance_put(n,name_source,price_source,resolved_type)
        return jsonify(ok=True,number=n,name_ko=name,price=price,currency="KRW",
                       name_source=name_source,price_source=price_source,
                       price_type=resolved_type,cache_hit=True,
                       validation="cache-first-v87")

    # Cache miss: use existing enrichment once; it persists successful results to Supabase.
    item,diag=_merge_kr_sources(n)
    if item:
        resolved_name_source=item.get("name_source") or name_source
        resolved_price_source=item.get("price_source")
        resolved_price_type=item.get("price_type")
        # v87: every successful discovery becomes reusable catalog data.
        # Only already-filtered/trusted metadata from _merge_kr_sources reaches this point.
        if sb_enabled():
            sb_upsert([{"set_number":n,
                        "name_ko":item.get("name_ko") or name,
                        "price_krw":item.get("price"),
                        "source":item.get("source"),
                        "source_url":item.get("source_url"),
                        "checked_at":item.get("checked_at") or time.strftime("%Y-%m-%d")}])
        sb_provenance_put(n,resolved_name_source,resolved_price_source,resolved_price_type)
        return jsonify(ok=True,number=n,name_ko=item.get("name_ko") or name,
                       price=item.get("price"),currency="KRW",
                       name_source=resolved_name_source,
                       price_source=resolved_price_source,
                       price_type=resolved_price_type,cache_hit=False,
                       diagnostics=diag,validation="cache-first-v87")

    return jsonify(ok=True,number=n,name_ko=name,price=None,currency="KRW",
                   name_source=name_source,price_source=None,price_type=None,
                   cache_hit=False,validation="cache-first-v87")

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
    out={}; failed=[]
    if not KEY:
        return jsonify(ok=False,error="BRICKSET_API_KEY not configured"),503

    def one(n):
        try:
            params=json.dumps({"setNumber":n+"-1","pageSize":1})
            r=requests.get(API+"/getSets",
                           params={"apiKey":KEY,"userHash":"","params":params},
                           timeout=10)
            if not r.ok:
                return n,None,"brickset_http_"+str(r.status_code)
            d=r.json()
            item=(d.get("sets") or [None])[0]
            if not item:
                return n,None,"set_not_found"
            k=cat.get(n) or {}
            return n,{
                "number":n,
                "name":item.get("name"),
                "year":item.get("year"),
                "theme":item.get("theme"),
                "pieces":item.get("pieces"),
                "launchDate":item.get("launchDate"),
                "image":(item.get("image") or {}).get("imageURL") or (item.get("image") or {}).get("thumbnailURL"),
                "LEGOCom":item.get("LEGOCom"),
                "kr":k or None
            },None
        except Exception as e:
            return n,None,type(e).__name__

    # v87: parallel requests prevent N owned sets from turning into an N*timeout request.
    targets=nums[:100]
    if targets:
        with ThreadPoolExecutor(max_workers=min(6,len(targets))) as ex:
            futs=[ex.submit(one,n) for n in targets]
            for fut in as_completed(futs):
                n,item,err=fut.result()
                if item is not None: out[n]=item
                else: failed.append({"number":n,"error":err})
    return jsonify(ok=True,items=out,failed=failed,
                   requested=len(targets),updated=len(out),
                   catalog_count=len(cat))

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
    n=str(number); search="https://kream.co.kr/search"
    h={"User-Agent":"Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/143 Mobile Safari/537.36",
       "Accept-Language":"ko-KR,ko;q=0.9"}
    try:
        r=requests.get(search,params={"keyword":n},headers=h,timeout=10)
        if not r.ok or _bad_page_text(r.text[:5000]): return None
        for u in _extract_detail_links(r.text,r.url,n,"kream.co.kr"):
            try:
                d=requests.get(u,headers=h,timeout=10)
                if not d.ok: continue
                name,price=_detail_page_metadata(d.text,n)
                if name or price is not None:
                    return {"name_ko":name,"price":price,"currency":"KRW",
                            "source":"KREAM 상세 발매정보",
                            "name_source":"KREAM 국내 표기" if name else None,
                            "source_url":d.url,"checked_at":time.strftime("%Y-%m-%d")}
            except Exception:
                continue
    except Exception:
        pass
    return None

KREAM_MODEL_ALIASES={
    # LEGO set/catalog number -> Korean retail/model number used by KREAM.
    # 5009609 is the LEGO set number; 6601584 is the KR alternate item/model number.
    "5009609":["6601584"],
}

KREAM_TRADES_TABLE="lego_kream_trades"
KREAM_MAX_HISTORY_PAGES=40


def _kream_headers(referer=None, accept_json=False):
    h={
        "User-Agent":"Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/143 Mobile Safari/537.36",
        "Accept-Language":"ko-KR,ko;q=0.9,en;q=0.7"
    }
    if accept_json: h["Accept"]="application/json, text/plain, */*"
    if referer: h["Referer"]=referer
    return h


def _parse_iso_utc(value):
    if not value: return None
    try:
        return datetime.fromisoformat(str(value).replace("Z","+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _kst_display_from_iso(value):
    dt=_parse_iso_utc(value)
    if not dt: return None
    return dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")


def _fallback_trade_time(text):
    """Convert KREAM public-page date text to an ISO timestamp when possible."""
    raw=str(text or "").strip()
    kst=timezone(timedelta(hours=9))
    now=datetime.now(kst)
    m=re.fullmatch(r"(\d{2})/(\d{2})/(\d{2})",raw)
    if m:
        try:
            return datetime(2000+int(m.group(1)),int(m.group(2)),int(m.group(3)),12,0,tzinfo=kst).astimezone(timezone.utc).isoformat().replace("+00:00","Z")
        except Exception: return None
    m=re.fullmatch(r"(\d+)\s*분\s*전",raw)
    if m: return (now-timedelta(minutes=int(m.group(1)))).astimezone(timezone.utc).isoformat().replace("+00:00","Z")
    m=re.fullmatch(r"(\d+)\s*시간\s*전",raw)
    if m: return (now-timedelta(hours=int(m.group(1)))).astimezone(timezone.utc).isoformat().replace("+00:00","Z")
    m=re.fullmatch(r"(\d+)\s*일\s*전",raw)
    if m: return (now-timedelta(days=int(m.group(1)))).astimezone(timezone.utc).isoformat().replace("+00:00","Z")
    return None


def _kream_product_context(number):
    """Find exact KREAM product detail page and product_id for a LEGO model number."""
    n=str(number).strip().split("-")[0]
    accepted=[n]+[x for x in KREAM_MODEL_ALIASES.get(n,[]) if x!=n]
    for query_number in accepted:
        search=f"https://kream.co.kr/search?keyword={query_number}"
        try:
            r=requests.get(search,headers=_kream_headers(),timeout=10)
            if not r.ok or _bad_page_text((r.text or "")[:5000]):
                continue
            for u in _extract_detail_links(r.text,r.url,query_number,"kream.co.kr")[:6]:
                try:
                    d=requests.get(u,headers=_kream_headers(u),timeout=10)
                    if not d.ok: continue
                    text=_decode_jsonish(d.text or "")
                    matched=next((x for x in accepted if re.search(rf"(?<!\d){re.escape(x)}(?!\d)",text)),None)
                    if not matched: continue
                    pm=re.search(r"/products/(\d+)",d.url or u)
                    if not pm:
                        pm=re.search(r'"productID"\s*:\s*"?(\d+)"?',text,re.I)
                    if not pm:
                        pm=re.search(r'"product_id"\s*:\s*(\d+)',text,re.I)
                    if not pm: continue
                    return {
                        "number":n,"model_number":matched,"product_id":int(pm.group(1)),
                        "source_url":d.url or u,"html":d.text or ""
                    }
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _kream_trade_table_ready():
    if not sb_enabled(): return False
    try:
        r=requests.get(f"{SUPABASE_URL}/rest/v1/{KREAM_TRADES_TABLE}",headers=_sb_headers(),
                       params={"select":"id","limit":"1"},timeout=4)
        return r.ok
    except Exception:
        return False


def _kream_latest_stored_trade_at(number):
    if not _kream_trade_table_ready(): return None
    n=re.sub(r"[^0-9]","",str(number))
    try:
        r=requests.get(f"{SUPABASE_URL}/rest/v1/{KREAM_TRADES_TABLE}",headers=_sb_headers(),
                       params={"set_number":f"eq.{n}","select":"trade_at","order":"trade_at.desc","limit":"1"},timeout=5)
        if r.ok and r.json(): return r.json()[0].get("trade_at")
    except Exception: pass
    return None


def _kream_trade_upsert_many(rows):
    if not rows or not _kream_trade_table_ready(): return 0
    saved=0
    for i in range(0,len(rows),200):
        chunk=rows[i:i+200]
        try:
            r=requests.post(
                f"{SUPABASE_URL}/rest/v1/{KREAM_TRADES_TABLE}",
                headers=_sb_headers("resolution=merge-duplicates,return=minimal"),
                params={"on_conflict":"set_number,kream_product_id,trade_at,price_krw,option_name"},
                json=chunk,timeout=12)
            if r.ok: saved+=len(chunk)
        except Exception:
            pass
    return saved


def _kream_stored_trades(number,limit=5000):
    if not _kream_trade_table_ready(): return []
    n=re.sub(r"[^0-9]","",str(number))
    try:
        r=requests.get(f"{SUPABASE_URL}/rest/v1/{KREAM_TRADES_TABLE}",headers=_sb_headers(),
                       params={"set_number":f"eq.{n}",
                               "select":"set_number,kream_product_id,model_number,price_krw,option_name,trade_at,source_url",
                               "order":"trade_at.asc","limit":str(min(max(int(limit),1),10000))},timeout=10)
        return r.json() if r.ok else []
    except Exception: return []


def _kream_public_html_trades(ctx):
    """Fallback to the public visible completed-trade rows when the JSON sales API is unavailable."""
    text=_decode_jsonish(ctx.get("html") or "")
    plain=re.sub(r"<[^>]+>"," ",text)
    plain=re.sub(r"&nbsp;"," ",plain,flags=re.I)
    plain=re.sub(r"\s+"," ",plain)
    pos=plain.find("체결 거래")
    if pos<0: return []
    area=plain[pos:pos+6000]
    rows=[]
    for m in re.finditer(r"([1-9][0-9]{0,2}(?:,[0-9]{3})+)\s*원\s*((?:\d{2}/\d{2}/\d{2})|(?:\d+\s*(?:분|시간|일)\s*전))",area):
        try: price=int(m.group(1).replace(",",""))
        except Exception: continue
        trade_at=_fallback_trade_time(m.group(2))
        if not trade_at: continue
        rows.append({
            "set_number":ctx["number"],"kream_product_id":ctx["product_id"],
            "model_number":ctx.get("model_number"),"price_krw":price,"option_name":"ONE SIZE",
            "trade_at":trade_at,"source_url":ctx.get("source_url")
        })
    rows.sort(key=lambda x:x["trade_at"],reverse=True)
    return rows


def _kream_fetch_sales(ctx,full_history=True):
    """Fetch completed transactions. Initial sync walks all pages; later sync stops at newest stored trade."""
    pid=ctx["product_id"]
    detail=ctx.get("source_url") or f"https://kream.co.kr/products/{pid}"
    stop_at=_parse_iso_utc(_kream_latest_stored_trade_at(ctx["number"])) if full_history else None
    rows=[]; complete=True; used_api=False; cursor=1; pages=0
    while cursor and pages<KREAM_MAX_HISTORY_PAGES:
        pages+=1
        try:
            r=requests.get(f"https://kream.co.kr/api/p/products/{pid}/sales",
                           params={"cursor":cursor,"per_page":50,"sort":"date_created[desc]"},
                           headers=_kream_headers(detail,True),timeout=10)
            if not r.ok: raise RuntimeError(f"http {r.status_code}")
            data=r.json() or {}; used_api=True
        except Exception:
            complete=False
            if not rows:
                rows=_kream_public_html_trades(ctx)
            break
        items=data.get("items") or []
        if not items: break
        reached_old=False
        for item in items:
            trade_at=item.get("date_created")
            dt=_parse_iso_utc(trade_at)
            if not dt: continue
            if stop_at and dt<=stop_at:
                reached_old=True
                continue
            try: price=int(round(float(item.get("price") or 0)))
            except Exception: price=0
            if price<1000 or price>10000000: continue
            option=str(item.get("option") or ((item.get("product_option") or {}).get("name_display")) or "")
            rows.append({
                "set_number":ctx["number"],"kream_product_id":pid,
                "model_number":ctx.get("model_number"),"price_krw":price,
                "option_name":option,"trade_at":trade_at,"source_url":detail
            })
        if reached_old: break
        nxt=data.get("next_cursor")
        if not full_history or not nxt: break
        cursor=nxt
    if pages>=KREAM_MAX_HISTORY_PAGES and cursor:
        complete=False
    rows.sort(key=lambda x:x["trade_at"],reverse=True)
    return rows,complete,used_api


def _kream_fetch_chart(ctx):
    pid=ctx["product_id"]
    detail=ctx.get("source_url") or f"https://kream.co.kr/products/{pid}"
    try:
        r=requests.get(f"https://kream.co.kr/api/p/products/{pid}/chart",
                       headers=_kream_headers(detail,True),timeout=10)
        if not r.ok: return {}
        d=r.json() or {}; out={}
        for block in d.get("charts") or []:
            span=str(block.get("span") or "")
            if span not in ("1m","3m","6m","1y","all"): continue
            arr=[]
            for p in block.get("data") or []:
                try: val=int(round(float(p.get("value") or 0)))
                except Exception: val=0
                t=str(p.get("time") or "")
                if t and val>0: arr.append({"time":t,"value":val})
            out[span]=arr
        return out
    except Exception:
        return {}


def _chart_from_trades(trades):
    """Fallback daily close-style chart from exact completed trades."""
    daily={}
    for row in trades:
        dt=_parse_iso_utc(row.get("trade_at"))
        if not dt: continue
        day=dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
        # rows are chronological from DB; later trade on the same day replaces prior one.
        daily[day]=int(row.get("price_krw") or 0)
    all_data=[{"time":d+"T00:00:00+09:00","value":p} for d,p in sorted(daily.items()) if p>0]
    if not all_data: return {}
    now=datetime.now(timezone(timedelta(hours=9))).date()
    spans={"all":all_data}
    for key,days in (("1m",31),("3m",93),("6m",186),("1y",366)):
        cutoff=now-timedelta(days=days)
        spans[key]=[x for x in all_data if _parse_iso_utc(x["time"]).astimezone(timezone(timedelta(hours=9))).date()>=cutoff]
    return spans


def _kream_sync_market(number,full_history=True,include_chart=False):
    n=str(number).strip().split("-")[0]
    ctx=_kream_product_context(n)
    if not ctx: return None
    new_rows,complete,used_api=_kream_fetch_sales(ctx,full_history=full_history)
    saved=_kream_trade_upsert_many(new_rows)
    # Latest is from fresh fetch; if none, use existing stored history so evaluation stays unchanged.
    stored=_kream_stored_trades(n,5000)
    combined=stored+new_rows
    uniq={}
    for row in combined:
        key=(str(row.get("trade_at")),int(row.get("price_krw") or 0),str(row.get("option_name") or ""))
        uniq[key]=row
    trades=sorted(uniq.values(),key=lambda x:str(x.get("trade_at") or ""))
    if not trades: return None
    latest=trades[-1]
    charts=(_kream_fetch_chart(ctx) or _chart_from_trades(trades)) if include_chart else {}
    return {
        "number":n,"model_number":ctx.get("model_number"),"product_id":ctx.get("product_id"),
        "price":int(latest.get("price_krw") or 0),"currency":"KRW",
        "price_type":"recent_trade","source":"KREAM 최근 체결가","source_url":ctx.get("source_url"),
        "trade_at":latest.get("trade_at"),"trade_date":_kst_display_from_iso(latest.get("trade_at")),
        "history_added":saved,"history_complete":bool(complete and used_api),
        "trade_count":len(trades),"charts":charts,
        "validation":"kream-sales-api-v87" if used_api else "kream-public-html-v87"
    }


def _kream_recent_trade_lookup(number):
    return _kream_sync_market(number,full_history=False,include_chart=False)


PRICE_HISTORY_TABLE="lego_price_history"

def price_history_put(number, price, source="KREAM 최근 체결가", source_url=None, checked_at=None):
    """Persist one market-price observation. Safe no-op if Supabase/table is unavailable."""
    if not sb_enabled():
        return False
    n=re.sub(r"[^0-9]","",str(number))
    try:
        p=int(price)
    except Exception:
        return False
    if not n or p<=0:
        return False
    payload={
        "set_number":n,
        "price_krw":p,
        "source":str(source or "KREAM 최근 체결가"),
        "source_url":source_url or None,
        "checked_at":checked_at or time.strftime("%Y-%m-%d")
    }
    try:
        u=f"{SUPABASE_URL}/rest/v1/{PRICE_HISTORY_TABLE}?on_conflict=set_number,checked_at,source"
        r=requests.post(
            u,
            headers={**_sb_headers(),"Prefer":"resolution=merge-duplicates,return=minimal"},
            json=payload,
            timeout=8
        )
        return r.ok
    except Exception:
        return False

@app.get("/api/price-history/<number>")
def api_price_history(number):
    n=re.sub(r"[^0-9]","",str(number))
    if not n:
        return jsonify(ok=False,error="invalid set number"),400
    rows=[]
    cloud=False
    if sb_enabled():
        try:
            u=f"{SUPABASE_URL}/rest/v1/{PRICE_HISTORY_TABLE}"
            r=requests.get(
                u,
                headers=_sb_headers(),
                params={
                    "set_number":f"eq.{n}",
                    "select":"set_number,price_krw,source,source_url,checked_at,created_at",
                    "order":"checked_at.asc,created_at.asc",
                    "limit":"365"
                },
                timeout=8
            )
            if r.ok:
                rows=r.json() or []
                cloud=True
        except Exception:
            pass
    return jsonify(ok=True,number=n,items=rows,cloud=cloud)

@app.post("/api/kream-market-batch")
def api_kream_market_batch():
    body=request.get_json(silent=True) or {}
    raw=body.get("numbers") or []
    nums=[]
    for x in raw:
        n=re.sub(r"[^0-9]","",str(x))
        if n and n not in nums: nums.append(n)
    nums=nums[:8]
    items={}; failed=[]
    if nums:
        with ThreadPoolExecutor(max_workers=min(3,len(nums))) as ex:
            futs={ex.submit(_kream_sync_market,n,True,False):n for n in nums}
            for fut in as_completed(futs):
                n=futs[fut]
                try: row=fut.result()
                except Exception: row=None
                if row: items[n]=row
                else: failed.append(n)
    return jsonify(ok=True,items=items,failed=failed,
                   requested=len(nums),updated=len(items),
                   trade_table_ready=_kream_trade_table_ready(),
                   validation="kream-full-history-v87")

@app.get("/api/kream-history/<number>")
def api_kream_history(number):
    n=re.sub(r"[^0-9]","",str(number))
    if not n: return jsonify(ok=False,error="invalid set number"),400
    sync=request.args.get("sync")=="1"
    live=None
    if sync:
        try: live=_kream_sync_market(n,True,True)
        except Exception: live=None
    trades=_kream_stored_trades(n,5000)
    ctx=None; charts={}
    if live:
        charts=live.get("charts") or {}
    else:
        try:
            ctx=_kream_product_context(n)
            if ctx: charts=_kream_fetch_chart(ctx)
        except Exception: charts={}
    if not charts: charts=_chart_from_trades(trades)
    recent=sorted(trades,key=lambda x:str(x.get("trade_at") or ""),reverse=True)[:100]
    for row in recent:
        row["trade_date"]=_kst_display_from_iso(row.get("trade_at"))
    latest=recent[0] if recent else None
    return jsonify(ok=True,number=n,items=recent,trade_count=len(trades),charts=charts,
                   latest=latest,trade_table_ready=_kream_trade_table_ready(),
                   history_complete=(live.get("history_complete") if live else None),
                   validation="kream-history-v87")


def _danawa_kr_lookup(number):
    """Exact set-number Danawa detail lookup. Search page is discovery only."""
    n=str(number); search="https://search.danawa.com/mobile/dsearch.php"
    h={"User-Agent":"Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/143 Mobile Safari/537.36",
       "Accept-Language":"ko-KR,ko;q=0.9"}
    try:
        r=requests.get(search,params={"keyword":n},headers=h,timeout=10)
        if not r.ok or _bad_page_text(r.text[:5000]): return None
        for u in _extract_detail_links(r.text,r.url,n,"danawa.com"):
            try:
                d=requests.get(u,headers=h,timeout=10)
                if not d.ok: continue
                name,_price=_detail_page_metadata(d.text,n)
                if name:
                    return {"name_ko":name,"price":None,"currency":"KRW",
                            "source":"다나와 국내 표기","name_source":"다나와 국내 표기",
                            "source_url":d.url,"checked_at":time.strftime("%Y-%m-%d")}
            except Exception:
                continue
    except Exception:
        pass
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
    untrusted_stored_name=bool(original_stored_name and
        (("KREAM" in stored_source and "LEGO Korea" not in stored_source) or
         ("다나와" in stored_source and "LEGO Korea" not in stored_source)))
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
          or brick.get("name_ko") or stored.get("name_ko")
          or danawa.get("name_ko") or kream.get("name_ko"))
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
    if instruction_name and not official.get("name_ko") and not verified.get("name_ko"):
        if price is not None and kream.get("price") is not None:
            source="LEGO Korea 조립설명서 + KREAM 가격"
        elif price is not None and brick.get("price") is not None:
            source="LEGO Korea 조립설명서 + 한국 가격 참고"
        else:
            source="LEGO Korea 조립설명서"
        source_url=instruction_url or source_url
    # v87: keep name provenance and price provenance independent.
    if official.get("name_ko"): name_source="LEGO Korea 공식"
    elif instruction_name: name_source="LEGO Korea 조립설명서"
    elif verified.get("name_ko"): name_source=verified.get("source") or "검증 한국 카탈로그"
    elif brick.get("name_ko"): name_source=brick.get("source") or "국내 판매자료"
    elif stored.get("name_ko"): name_source=stored.get("source")
    elif danawa.get("name_ko"): name_source=danawa.get("name_source") or "다나와 국내 표기"
    elif kream.get("name_ko"): name_source=kream.get("name_source") or "KREAM 국내 표기"
    else: name_source=None

    if official.get("price") is not None:
        price_source="LEGO Korea"; price_type="official_msrp"
    elif verified.get("price") is not None:
        price_source=verified.get("source") or "국내 판매자료"
        price_type="official_msrp" if "LEGO Korea" in str(price_source) else "release_price"
    elif kream.get("price") is not None:
        price_source="KREAM"; price_type="release_price"
    elif brick.get("price") is not None:
        price_source=brick.get("source") or "국내 판매자료"; price_type="release_price"
    elif stored.get("price_krw") is not None:
        price_source=stored.get("source"); price_type="release_price"
    else:
        price_source=None; price_type=None

    item=None
    if name or price is not None:
        item={"name_ko":name,"price":price,"currency":"KRW","source":source,
              "source_url":source_url,"name_source":name_source,
              "price_source":price_source,"price_type":price_type,
              "checked_at":time.strftime("%Y-%m-%d")}
    def usable(x): return bool(x and (x.get("name_ko") or x.get("price") is not None))
    diag={"official":usable(official),"instructions":bool(instruction_name),
          "kream":usable(kream),"brickmecha":usable(brick),"danawa":usable(danawa),
          "stored":bool(stored.get("name_ko") or stored.get("price_krw") is not None),
          "verified":bool(verified),"validation":"brickset-ko-overlay-v87"}
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
    """v40: no Render-side LEGO.com crawling.
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
    """v87: Render->LEGO is HTTP 403. Use the verified indexed KR catalog instead."""
    n=str(number).strip().split("-")[0]
    row=KR_CATALOG.get(n) if "KR_CATALOG" in globals() else None
    if row and row.get("name_ko"):
        return row.get("name_ko"), row.get("source_url")
    return None, None

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

@app.get("/api/kr-name-diagnostic/<number>")
def api_kr_name_diagnostic(number):
    n=re.sub(r"[^0-9]","",str(number))
    if not n:
        return jsonify(ok=False,error="invalid set number"),400

    urls=[
      f"https://www.lego.com/ko-kr/service/building-instructions/{n}",
      f"https://www.lego.com/ko-kr/service/building-instructions/search-results?searchString={n}"
    ]
    checks=[]
    headers={"User-Agent":"Mozilla/5.0","Accept-Language":"ko-KR,ko;q=0.9,en;q=0.7"}
    for url in urls:
        try:
            r=requests.get(url,headers=headers,timeout=12,allow_redirects=True)
            text=r.text or ""
            checks.append({
              "url":url,"status":r.status_code,"final_url":r.url,
              "bytes":len(r.content or b""),"set_number_found":n in text,
              "korean_found":bool(re.search(r"[가-힣]{2,}",text)),
              "blocked":any(x.lower() in text.lower() for x in ["access denied","blocked","captcha","robot"])
            })
        except Exception as e:
            checks.append({"url":url,"error":type(e).__name__})
    try:
        extracted=_instruction_name(n)
    except Exception as e:
        extracted=None
        extract_error=type(e).__name__
    else:
        extract_error=None
    return jsonify(ok=True,number=n,checks=checks,extracted=extracted,
                   extract_error=extract_error,validation="kr-name-diagnostic-v87")


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


RELATION_TABLE="lego_set_relations"

def relation_put(primary_number, related_number, relation_type, source="Brickset", evidence=""):
    if not sb_enabled(): return False
    payload={"set_number":str(primary_number),"related_set_number":str(related_number),
             "relation_type":str(relation_type),"relation_label":str(relation_type).upper(),
             "source":source,"source_url":"",
             "updated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
    try:
        u=f"{SUPABASE_URL}/rest/v1/{RELATION_TABLE}?on_conflict=set_number,related_set_number,relation_type"
        r=requests.post(u,headers={**_sb_headers(),"Prefer":"resolution=merge-duplicates,return=minimal"},json=payload,timeout=6)
        return r.ok
    except Exception:
        return False

_V60_SEEDED=False
def _v87_seed_official_catalog():
    global _V60_SEEDED
    if _V60_SEEDED or not sb_enabled():
        return
    _V60_SEEDED=True
    try:
        batch=[]
        for n,row in load_kr_catalog().items():
            src=str((row or {}).get("source") or "")
            if "LEGO Korea 공식" not in src:
                continue
            batch.append({"set_number":str(n),
                          "name_ko":row.get("name_ko"),
                          "price_krw":row.get("price"),
                          "source":"LEGO Korea 공식",
                          "source_url":row.get("source_url"),
                          "checked_at":row.get("checked_at") or time.strftime("%Y-%m-%d")})
        if batch:
            sb_upsert(batch)
            for r in batch:
                sb_provenance_put(r["set_number"],"LEGO Korea","LEGO Korea","official_msrp")
    except Exception:
        pass

@app.before_request
def _v87_bootstrap_catalog():
    _v87_seed_official_catalog()


@app.get("/api/relations/<number>")
def api_relations(number):
    n=re.sub(r"[^0-9]","",str(number or ""))
    if not n:
        return jsonify(ok=False,error="invalid set number"),400
    rows=[]
    if sb_enabled():
        try:
            url=f"{SUPABASE_URL}/rest/v1/lego_set_relations"
            params={"or":f"(set_number.eq.{n},related_set_number.eq.{n})","select":"*"}
            r=requests.get(url,headers=sb_headers(),params=params,timeout=8)
            if r.ok: rows=r.json() or []
        except Exception:
            pass
    return jsonify(ok=True,number=n,relations=rows)



# =========================
# v87 Master LEGO catalog
# =========================
MASTER_TABLE="lego_master_catalog"
MASTER_STATE_TABLE="lego_catalog_sync_state"
MASTER_FILTER_CACHE={"t":0,"themes":[],"years":[]}
MASTER_OVERLAY_CACHE={"t":0,"data":{}}
MASTER_SYNC_THREAD=None
MASTER_SYNC_RUNTIME={"running":False,"message":"idle","last_result":None,"started_at":None}
MASTER_SYNC_LOCK=threading.Lock()

BL_CONSUMER_KEY=os.getenv("BRICKLINK_CONSUMER_KEY","")
BL_CONSUMER_SECRET=os.getenv("BRICKLINK_CONSUMER_SECRET","")
BL_TOKEN_VALUE=os.getenv("BRICKLINK_TOKEN_VALUE","")
BL_TOKEN_SECRET=os.getenv("BRICKLINK_TOKEN_SECRET","")
BL_BASE="https://api.bricklink.com/api/store/v1"

def bricklink_configured():
    return all([BL_CONSUMER_KEY,BL_CONSUMER_SECRET,BL_TOKEN_VALUE,BL_TOKEN_SECRET])

def _master_headers(prefer=None):
    return _sb_headers(prefer)

def master_table_ready():
    if not sb_enabled(): return False
    try:
        r=requests.get(f"{SUPABASE_URL}/rest/v1/{MASTER_TABLE}",headers=_master_headers(),
                       params={"select":"set_number","limit":"1"},timeout=4)
        return r.ok
    except Exception:
        return False

def master_state_get(key,default=None):
    if not sb_enabled(): return default
    try:
        r=requests.get(f"{SUPABASE_URL}/rest/v1/{MASTER_STATE_TABLE}",headers=_master_headers(),
                       params={"select":"value","key":f"eq.{key}","limit":"1"},timeout=4)
        if r.ok and r.json(): return r.json()[0].get("value",default)
    except Exception: pass
    return default

def master_state_set(key,value):
    if not sb_enabled(): return False
    payload={"key":key,"value":value,"updated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
    try:
        r=requests.post(f"{SUPABASE_URL}/rest/v1/{MASTER_STATE_TABLE}",
                        headers=_master_headers("resolution=merge-duplicates,return=minimal"),
                        params={"on_conflict":"key"},json=payload,timeout=6)
        return r.ok
    except Exception: return False

def _master_kr_overlays():
    now=time.time()
    if now-MASTER_OVERLAY_CACHE.get("t",0)<600:
        return MASTER_OVERLAY_CACHE.get("data",{})
    data={}
    for n,row in load_kr_catalog().items():
        data[str(n)]={"name_ko":(row or {}).get("name_ko"),"price_krw":(row or {}).get("price")}
    if sb_enabled():
        try:
            r=requests.get(f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",headers=_master_headers(),
                           params={"select":"set_number,name_ko,price_krw","limit":"2000"},timeout=8)
            if r.ok:
                for row in r.json() or []:
                    n=str(row.get("set_number") or "")
                    if n:
                        prev=data.get(n,{})
                        data[n]={"name_ko":row.get("name_ko") or prev.get("name_ko"),
                                 "price_krw":row.get("price_krw") if row.get("price_krw") is not None else prev.get("price_krw")}
        except Exception: pass
    MASTER_OVERLAY_CACHE.update({"t":now,"data":data})
    return data

def _master_row(item,overlays=None):
    full=str(item.get("number") or "")
    n=full.split("-")[0]
    image=item.get("image") or {}
    lego=item.get("LEGOCom") or {}
    ext=item.get("extendedData") or {}
    ov=(overlays or {}).get(n,{})
    def retail(region):
        try: return (lego.get(region) or {}).get("retailPrice")
        except Exception: return None
    return {
        "set_number":n,"brickset_number":full,"set_id":item.get("setID"),
        "name_en":item.get("name"),"name_ko":ov.get("name_ko"),
        "theme":item.get("theme"),"subtheme":item.get("subtheme"),
        "category":item.get("category"),"year":item.get("year"),
        "pieces":item.get("pieces"),"minifigs":item.get("minifigs"),
        "released":item.get("released"),"image_url":image.get("imageURL"),
        "thumbnail_url":image.get("thumbnailURL"),"brickset_url":item.get("bricksetURL"),
        "retail_us":retail("US"),"retail_uk":retail("UK"),"retail_de":retail("DE"),
        "price_krw":ov.get("price_krw"),"launch_date":item.get("launchDate"),
        "exit_date":item.get("exitDate"),"availability":item.get("availability"),
        "description":ext.get("description"),"brickset_last_updated":item.get("lastUpdated"),
        "updated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
    }

def master_upsert(rows):
    if not rows or not sb_enabled(): return False
    ok=True
    for i in range(0,len(rows),200):
        chunk=rows[i:i+200]
        try:
            r=requests.post(f"{SUPABASE_URL}/rest/v1/{MASTER_TABLE}",
                headers=_master_headers("resolution=merge-duplicates,return=minimal"),
                params={"on_conflict":"set_number"},json=chunk,timeout=20)
            if not r.ok: ok=False
        except Exception: ok=False
    return ok

def master_name_search(q,limit=20):
    if not master_table_ready(): return []
    raw=str(q or "").strip()
    safe=re.sub(r"[^0-9A-Za-z가-힣 _-]"," ",raw).strip()
    if not safe: return []
    try:
        r=requests.get(f"{SUPABASE_URL}/rest/v1/{MASTER_TABLE}",headers=_master_headers(),
            params={"select":"set_number,name_en,name_ko,theme,year",
                    "or":f"(name_ko.ilike.*{safe}*,name_en.ilike.*{safe}*,set_number.ilike.*{safe}*)",
                    "limit":str(min(int(limit),50))},timeout=5)
        return r.json() if r.ok else []
    except Exception: return []

def _brickset_years():
    try:
        r=requests.get(API+"/getYears",params={"apiKey":KEY,"theme":""},timeout=12)
        if not r.ok: return []
        years=[]
        for row in r.json().get("years",[]) or []:
            y=row.get("year") if isinstance(row,dict) else None
            try: yi=int(y)
            except Exception: continue
            if yi not in years: years.append(yi)
        return sorted(years,reverse=True)
    except Exception: return []

def _brickset_sync_page(params):
    r=requests.get(API+"/getSets",params={"apiKey":KEY,"userHash":"","params":json.dumps(params)},timeout=25)
    r.raise_for_status()
    d=r.json()
    if d.get("status")=="error":
        raise RuntimeError(d.get("message") or "Brickset error")
    return d

def run_master_sync(max_calls=20):
    if not KEY: return {"ok":False,"error":"BRICKSET_API_KEY missing"}
    if not master_table_ready(): return {"ok":False,"error":"master_table_missing"}
    max_calls=max(1,min(int(max_calls or 20),40))
    if not MASTER_SYNC_LOCK.acquire(blocking=False):
        return {"ok":False,"error":"sync_already_running"}
    try:
        calls=0; rows_saved=0
        overlays=_master_kr_overlays()
        initial_complete=bool(master_state_get("initial_complete",False))
        if not initial_complete:
            years=master_state_get("years",None)
            if not isinstance(years,list) or not years:
                years=_brickset_years()
                if not years: return {"ok":False,"error":"years_lookup_failed"}
                master_state_set("years",years)
            yi=int(master_state_get("year_index",0) or 0)
            page=int(master_state_get("page",1) or 1)
            while calls<max_calls and yi<len(years):
                year=int(years[yi])
                d=_brickset_sync_page({"year":year,"pageSize":500,"pageNumber":page,"extendedData":1,"orderBy":"Number"})
                calls+=1
                sets=d.get("sets",[]) or []
                rows=[_master_row(x,overlays) for x in sets if x.get("number")]
                if rows:
                    master_upsert(rows); rows_saved+=len(rows)
                matches=int(d.get("matches") or len(sets))
                pages=max(1,math.ceil(matches/500))
                MASTER_SYNC_RUNTIME["message"]=f"{year}년 {page}/{pages} 페이지 동기화"
                if page>=pages:
                    yi+=1; page=1
                else:
                    page+=1
                master_state_set("year_index",yi); master_state_set("page",page)
                master_state_set("last_progress",{"year":year,"page":page,"calls":calls,"rows_saved":rows_saved})
            if yi>=len(years):
                initial_complete=True
                master_state_set("initial_complete",True)
                master_state_set("last_sync_date",time.strftime("%Y-%m-%d"))
            return {"ok":True,"mode":"initial","calls":calls,"rows_saved":rows_saved,
                    "initial_complete":initial_complete,"year_index":yi,"years_total":len(years)}
        # Incremental update after initial mirror.
        last=str(master_state_get("last_sync_date",time.strftime("%Y-%m-%d")) or time.strftime("%Y-%m-%d"))
        page=1
        while calls<max_calls:
            d=_brickset_sync_page({"updatedSince":last,"pageSize":500,"pageNumber":page,"extendedData":1,"orderBy":"Number"})
            calls+=1
            sets=d.get("sets",[]) or []
            rows=[_master_row(x,overlays) for x in sets if x.get("number")]
            if rows:
                master_upsert(rows); rows_saved+=len(rows)
            matches=int(d.get("matches") or len(sets)); pages=max(1,math.ceil(matches/500))
            if page>=pages: break
            page+=1
        master_state_set("last_sync_date",time.strftime("%Y-%m-%d"))
        return {"ok":True,"mode":"incremental","calls":calls,"rows_saved":rows_saved,"initial_complete":True}
    except Exception as e:
        return {"ok":False,"error":str(e)[:250]}
    finally:
        MASTER_SYNC_LOCK.release()

def _bl_pct(v):
    return urllib.parse.quote(str(v),safe="~-._")

def _bricklink_get(path):
    if not bricklink_configured(): return None,None
    url=BL_BASE+path
    oauth={"oauth_consumer_key":BL_CONSUMER_KEY,"oauth_token":BL_TOKEN_VALUE,
           "oauth_signature_method":"HMAC-SHA1","oauth_timestamp":str(int(time.time())),
           "oauth_nonce":secrets.token_hex(8),"oauth_version":"1.0"}
    param_str="&".join(f"{_bl_pct(k)}={_bl_pct(v)}" for k,v in sorted(oauth.items()))
    base_str="GET&"+_bl_pct(url)+"&"+_bl_pct(param_str)
    key=_bl_pct(BL_CONSUMER_SECRET)+"&"+_bl_pct(BL_TOKEN_SECRET)
    sig=base64.b64encode(hmac.new(key.encode(),base_str.encode(),hashlib.sha1).digest()).decode()
    oauth["oauth_signature"]=sig
    auth="OAuth "+", ".join(f'{_bl_pct(k)}="{_bl_pct(v)}"' for k,v in oauth.items())
    try:
        r=requests.get(url,headers={"Authorization":auth},timeout=12)
        return r,(r.json() if r.text else None)
    except Exception: return None,None

def run_bricklink_enrich(max_items=20):
    if not bricklink_configured(): return {"ok":False,"error":"bricklink_not_configured"}
    if not master_table_ready(): return {"ok":False,"error":"master_table_missing"}
    try:
        r=requests.get(f"{SUPABASE_URL}/rest/v1/{MASTER_TABLE}",headers=_master_headers(),
            params={"select":"set_number,brickset_number","bricklink_checked_at":"is.null",
                    "order":"year.desc","limit":str(min(max(1,int(max_items)),50))},timeout=8)
        rows=r.json() if r.ok else []
    except Exception: rows=[]
    updated=0
    for row in rows:
        n=str(row.get("set_number") or ""); full=str(row.get("brickset_number") or (n+"-1"))
        if "-" not in full: full=n+"-1"
        resp,data=_bricklink_get("/items/SET/"+urllib.parse.quote(full,safe="-"))
        payload={"set_number":n,"bricklink_checked_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
        if resp is not None and resp.ok and isinstance(data,dict) and data.get("data"):
            item=data.get("data") or {}
            payload.update({"bricklink_number":item.get("no") or full,"bricklink_name":item.get("name"),
                            "bricklink_alt_no":item.get("alternate_no"),"bricklink_image_url":item.get("image_url"),
                            "bricklink_status":"ok"})
            updated+=1
        else:
            payload["bricklink_status"]="not_found"
        master_upsert([payload])
    return {"ok":True,"checked":len(rows),"updated":updated}

def _master_sync_worker():
    global MASTER_SYNC_THREAD
    MASTER_SYNC_RUNTIME.update({"running":True,"message":"Brickset 마스터 DB 동기화 중",
                                "started_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())})
    try:
        result=run_master_sync(20)
        MASTER_SYNC_RUNTIME["last_result"]=result
        MASTER_SYNC_RUNTIME["message"]="동기화 완료" if result.get("ok") else (result.get("error") or "동기화 실패")
        if result.get("ok") and result.get("initial_complete") and bricklink_configured():
            bl=run_bricklink_enrich(20)
            MASTER_SYNC_RUNTIME["bricklink_result"]=bl
    finally:
        MASTER_SYNC_RUNTIME["running"]=False

@app.post("/api/master-sync-background")
def master_sync_background():
    global MASTER_SYNC_THREAD
    if not master_table_ready():
        return jsonify(ok=False,error="master_table_missing"),200
    if MASTER_SYNC_THREAD is not None and MASTER_SYNC_THREAD.is_alive():
        return jsonify(ok=True,started=False,already_running=True,status=MASTER_SYNC_RUNTIME)
    MASTER_SYNC_THREAD=threading.Thread(target=_master_sync_worker,daemon=True,name="lego-master-sync")
    MASTER_SYNC_THREAD.start()
    return jsonify(ok=True,started=True,status=MASTER_SYNC_RUNTIME)

@app.get("/api/master-sync-status")
def master_sync_status():
    count=0
    if master_table_ready():
        try:
            r=requests.get(f"{SUPABASE_URL}/rest/v1/{MASTER_TABLE}",headers=_master_headers("count=exact"),
                           params={"select":"set_number","limit":"1"},timeout=5)
            cr=r.headers.get("Content-Range","")
            tail=cr.rsplit("/",1)[-1] if "/" in cr else ""
            if tail.isdigit(): count=int(tail)
        except Exception: pass
    return jsonify(ok=True,table_ready=master_table_ready(),count=count,
                   initial_complete=bool(master_state_get("initial_complete",False)),
                   progress=master_state_get("last_progress",{}),
                   last_sync_date=master_state_get("last_sync_date",None),
                   running=bool(MASTER_SYNC_RUNTIME.get("running")),
                   message=MASTER_SYNC_RUNTIME.get("message"),
                   bricklink_configured=bricklink_configured())

@app.get("/api/master-filters")
def master_filters():
    now=time.time()
    if now-MASTER_FILTER_CACHE.get("t",0)<21600 and MASTER_FILTER_CACHE.get("themes"):
        return jsonify(ok=True,themes=MASTER_FILTER_CACHE["themes"],years=MASTER_FILTER_CACHE["years"],cached=True)
    themes=[]; years=[]
    try:
        tr=requests.get(API+"/getThemes",params={"apiKey":KEY},timeout=10)
        if tr.ok:
            themes=sorted([str(x.get("theme")) for x in tr.json().get("themes",[]) if x.get("theme")])
    except Exception: pass
    years=_brickset_years()
    MASTER_FILTER_CACHE.update({"t":now,"themes":themes,"years":years})
    return jsonify(ok=True,themes=themes,years=years,cached=False)

@app.get("/api/master-catalog")
def master_catalog_api():
    if not master_table_ready():
        return jsonify(ok=False,error="master_table_missing",items=[],total=0),200
    q=(request.args.get("q") or "").strip()
    theme=(request.args.get("theme") or "").strip()
    year=(request.args.get("year") or "").strip()
    sort=(request.args.get("sort") or "year_desc").strip()
    try: page=max(1,int(request.args.get("page") or 1))
    except Exception: page=1
    try: size=max(10,min(60,int(request.args.get("page_size") or 30)))
    except Exception: size=30
    params={"select":"set_number,brickset_number,name_en,name_ko,theme,subtheme,year,pieces,minifigs,image_url,thumbnail_url,price_krw,retail_us,availability,brickset_url,bricklink_number,bricklink_name,bricklink_alt_no,bricklink_image_url"}
    if q:
        safe=re.sub(r"[^0-9A-Za-z가-힣 _-]"," ",q).strip()
        if safe:
            params["or"]=f"(set_number.ilike.*{safe}*,name_en.ilike.*{safe}*,name_ko.ilike.*{safe}*,bricklink_name.ilike.*{safe}*,bricklink_alt_no.ilike.*{safe}*)"
    if theme: params["theme"]="eq."+theme
    if year.isdigit(): params["year"]="eq."+year
    orders={"year_desc":"year.desc,name_en.asc","year_asc":"year.asc,name_en.asc","name":"name_en.asc","number":"set_number.asc","pieces_desc":"pieces.desc.nullslast"}
    params["order"]=orders.get(sort,orders["year_desc"])
    params["limit"]=str(size); params["offset"]=str((page-1)*size)
    try:
        r=requests.get(f"{SUPABASE_URL}/rest/v1/{MASTER_TABLE}",headers=_master_headers("count=exact"),params=params,timeout=10)
        if not r.ok: return jsonify(ok=False,error="master_query_failed",items=[],total=0),200
        total=0; cr=r.headers.get("Content-Range","")
        tail=cr.rsplit("/",1)[-1] if "/" in cr else ""
        if tail.isdigit(): total=int(tail)
        return jsonify(ok=True,items=r.json() or [],total=total,page=page,page_size=size,
                       pages=max(1,math.ceil(total/size)) if total else 1)
    except Exception as e:
        return jsonify(ok=False,error=type(e).__name__,items=[],total=0),200
