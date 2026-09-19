LEGO Collector v20
- 앱 실행 시 Brickset + LEGO Korea 자동 동기화
- LEGO Korea 신제품 페이지 + 전체상품 앞쪽 페이지에서 세트번호/한글명/원화가격 자동 발견 시도
- 발견 결과 서버 메모리 12시간 캐시
- 검증 kr_catalog.json > 자동발견 > 보유제품 개별 공식조회 순으로 적용
- 보유제품 한글명/한국 정가 자동 반영
- 수동 '카탈로그 적용', 'JSON 복사' 버튼 제거
- 자동수집 실패 시 기존 데이터 보존
- v19 이하 localStorage 자동 이전

주의:
LEGO Korea는 이 용도의 공개 가격 API가 확인되지 않아 웹페이지 자동 발견은 best-effort입니다.
Render 무료/일반 인스턴스 재시작 시 메모리 자동발견 캐시는 초기화되며 다음 앱 실행에서 다시 생성됩니다.
영구적으로 수천 세트 카탈로그를 누적하려면 외부 DB(Postgres/Supabase 등)가 필요합니다.

GitHub: index.html, server.py, kr_catalog.json 3개 파일 반영
