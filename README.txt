LEGO Collector v19
완전 자동화 1단계

앱 실행 시:
1) 보유 세트 Brickset 최신 기본정보 자동 동기화
2) 검증된 kr_catalog.json 자동 적용
3) 카탈로그에 없는 보유 세트는 LEGO Korea 공식 페이지에서 한글명/원화가격 자동 확인
4) 성공한 한국 정보는 브라우저 소장 데이터에 자동 저장
5) 실패해도 기존 데이터 유지
6) LEGO Korea 실시간 조회 결과는 서버 메모리에 6시간 캐시

중요:
LEGO Korea는 공개 가격 API가 확인되지 않아 공식 웹페이지 조회는 best-effort입니다.
공식 사이트 구조/봇 차단이 바뀌면 일부 세트는 해외정보 fallback이 유지됩니다.
검증된 kr_catalog.json 데이터가 항상 실시간 추출보다 우선합니다.

GitHub: index.html, server.py, kr_catalog.json 3개 파일 반영
