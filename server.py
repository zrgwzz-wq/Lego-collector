import os,json,requests
from flask import Flask,request,jsonify,send_from_directory
app=Flask(__name__); KEY=os.environ.get("BRICKSET_API_KEY","")
@app.get("/")
def home(): return send_from_directory(".","index.html")
@app.get("/api/health")
def health(): return jsonify(ok=True,key_configured=bool(KEY))
@app.get("/api/search")
def search():
    if not KEY:return jsonify(error="BRICKSET_API_KEY 미설정"),500
    p={"apiKey":KEY,"userHash":"","params":json.dumps({"query":request.args.get("q",""),"pageSize":20,"extendedData":1})}
    r=requests.get("https://brickset.com/api/v3.asmx/getSets",params=p,timeout=20);r.raise_for_status();return jsonify(r.json())
