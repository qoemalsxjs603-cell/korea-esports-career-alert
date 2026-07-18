# 맞춤형 e스포츠 채용 공고 수집기 v11

v10 실제 diagnostic 결과를 반영한 최종 안정화 버전입니다.

## 이번에 고친 항목

- 크래프톤: TOP, Family Site 오탐 제거. 공고 제목 자체에 `Esports`가 있을 때만 감지
- KeSPA: 일반 페이지 건강성 검사가 아니라 `notice_view + brd_id` 실제 게시물 번호로 감지
- 충남콘텐츠진흥원: 링크의 상위 카드에서 `진행중` 상태를 단계적으로 탐색
- 디플러스 기아: 잡코리아 회사 페이지가 정상이고 `GI_Read` 공고가 없으면 검증된 0건
- 한화생명e스포츠: 무거운 신규 포털 대신 같은 한화인 공식 도메인의 정적 채용목록 사용
- 텐센트: 화면 링크와 내장 JSON의 `postId/postName`을 함께 분석
- requests HTML이 불완전하면 브라우저 HTML로 자동 재검증
- 검증 실패 시 상태를 절대 0건으로 덮어쓰지 않음

## 실행 순서

1. 기존 저장소 파일을 v11 ZIP 내용으로 완전히 교체
2. Actions → Focused Esports Job Alert v11 → diagnostic
3. `data/last_run_report.json` 확인
4. normal을 한 번 실행해 현재 공고를 기준값으로 저장
5. 이후 2시간마다 검증된 신규 공고만 메일 발송

첫 normal 실행은 메일을 보내지 않습니다.
