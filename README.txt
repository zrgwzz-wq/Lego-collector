LEGO Collector v13
- GitHub 루트 index.html/server.py 교체 후 Commit
- v12 이하 데이터 자동 이전
- 신규 등록 시 LEGO Korea 한국 정가 자동 확인 시도
- 가격관리: 세트별 '정가 자동확인'
- 대시보드: '보유 세트 한국 정가 업데이트' 일괄 버튼
- LEGO Korea 공식 응답에서 세트번호와 KRW 가격이 함께 검증될 때만 저장
- 확인 실패 시 기존 Brickset 해외 기준/수동입력 fallback 유지
- 한국 정가 확인일과 출처 저장
주의: LEGO Korea는 이 앱을 위한 공개 가격 API를 제공하는 구조가 아니므로 사이트 구조가 바뀌면 자동확인이 실패할 수 있습니다. 실패 시 잘못된 가격을 저장하지 않고 fallback합니다.
