LEGO Collector v23
- LEGO Korea 공식 조립설명서 한국어 제품명 추출 강화
- 10305 공식명 '사자 기사의 성' 검증 시드 추가 (한국 정가는 확인되지 않아 비워둠)
- /api/kr-lookup/<세트번호> 추가: 찾은 정보는 Supabase에 자동 저장
- supabase_setup.sql에 service_role 권한 포함

GitHub 루트에서 index.html, server.py, kr_catalog.json, supabase_setup.sql 교체 후 Render 배포.
배포 후: https://lego-collector.onrender.com/api/kr-lookup/10305
