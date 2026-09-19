Render 배포:
1. GitHub에 이 폴더 업로드
2. Render > New > Web Service > 저장소 연결
3. Build: pip install -r requirements.txt
4. Start: gunicorn server:app
5. Environment > BRICKSET_API_KEY = 새 Brickset API Key
6. Deploy

API Key는 HTML에 저장되지 않고 서버 환경변수에서만 읽습니다.
상태: MISB / NIB / 중고 Complete. 동일 세트 개체별 등록 및 수량 자동 계산.
