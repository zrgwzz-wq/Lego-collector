LEGO Collector v21 — 영구 한국 카탈로그

핵심
- LEGO Korea 조립설명서 페이지를 공식 한글명 소스로 추가 (단종 세트 포함)
- 기존 LEGO Korea 실시간 가격 확인은 보조적으로 유지
- Supabase/Postgres 영구 저장 지원
- 앱 실행 시 자동 동기화
- 검증 repo 카탈로그 > Supabase DB > LEGO Korea 공식 조회 순서
- 새로 찾은 한글명/가격은 Supabase에 자동 upsert
- Supabase 미설정이어도 기존 기능은 그대로 동작

Supabase 설정
1. Supabase 프로젝트 생성
2. SQL Editor에서 supabase_setup.sql 실행
3. Render > Environment에 아래 두 값을 추가
   SUPABASE_URL = 프로젝트 URL
   SUPABASE_SERVICE_KEY = service_role/secret 서버 키
4. Render 재배포
주의: SUPABASE_SERVICE_KEY는 절대로 GitHub/index.html에 넣지 마세요.

GitHub 반영 파일:
index.html, server.py, kr_catalog.json, supabase_setup.sql
