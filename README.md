# Korea eSports Career Alert v7

한국 e스포츠 커리어 모니터링용 GitHub Actions 프로젝트입니다.

## 포함 범위

1. 게임사 및 게임 채용 사이트
   - 크래프톤, 스마일게이트, 넥슨, 넥슨게임즈, 엔씨소프트, 넷마블, 카카오게임즈, 펄어비스, 네오위즈, 컴투스, 웹젠, 시프트업 등
   - 게임잡, 사람인, 잡코리아 e스포츠 검색 URL 포함

2. e스포츠 팀
   - T1, Gen.G, 한화생명e스포츠, Dplus KIA, KT Rolster, DRX, DN FREECS, BNK FearX, 농심 레드포스, OK저축은행 브리온
   - Sports Job Alio 포함

3. 공공기관
   - JOB ALIO, Cleaneye Job+, ALIO Plus
   - 문화체육관광부, KOCCA, 한국e스포츠협회
   - 경기콘텐츠진흥원, 충남콘텐츠진흥원, 부산정보산업진흥원, 대전정보문화산업진흥원, 광주정보문화산업진흥원, 강원정보문화산업진흥원, 인천테크노파크 등

4. 교수/강사 초빙
   - Hibrain, JinhakPro, 전문대학 교원 채용, 사람인/잡코리아 교수·강사 검색

5. Tencent / NetEase / Garena
   - 공식 채용 페이지
   - LinkedIn 검색 URL
   - 한국 관련 키워드: 韩, 韩国, 韩语, Korea, Korean, South Korea, Seoul 등

## 첫 실행 방식

기본값은 `initial_notify_existing = false`입니다.

즉, 첫 실행 때 이미 올라와 있는 공고는 `seen_jobs.json`에 저장만 하고 메일은 보내지 않습니다.
두 번째 실행부터 새로 발견되는 공고만 메일로 보냅니다.

기존 공고도 바로 메일로 받고 싶으면:

`config/settings.json`에서 아래 값을 `true`로 바꾸세요.

```json
"initial_notify_existing": true
```

## 이메일 설정

GitHub 저장소에서:

Settings → Secrets and variables → Actions → New repository secret

Gmail 사용 시:

- `EMAIL_USER`: 발송용 Gmail 주소
- `EMAIL_PASSWORD`: Gmail 앱 비밀번호

네이버 메일 SMTP 사용 시:

- `SMTP_HOST`: smtp.naver.com
- `SMTP_PORT`: 587
- `SMTP_USER`: 발송용 네이버 메일 주소
- `SMTP_PASSWORD`: 네이버 SMTP 비밀번호 또는 앱 비밀번호
- `SMTP_FROM`: 발송용 네이버 메일 주소

이메일 설정이 없어도 실행은 됩니다. 그 경우 테스트 모드로 로그에만 알림 내용이 출력됩니다.

## 수정 방법

사이트를 추가하려면 아래 파일 중 하나를 수정하세요.

- `config/game_companies.json`
- `config/esports_teams.json`
- `config/public_orgs.json`
- `config/universities.json`

키워드를 수정하려면:

- `config/keywords.json`

실행 주기를 바꾸려면:

- `.github/workflows/career-alert.yml`

현재는 매시간 1회 실행입니다.

```yaml
schedule:
  - cron: "0 * * * *"
```

## 주의

- LinkedIn, 사람인, 잡코리아, 일부 공공기관 사이트는 봇 차단이나 동적 로딩 때문에 수집이 실패할 수 있습니다.
- 실패한 사이트는 건너뛰고 다른 사이트는 계속 실행되도록 만들었습니다.
- 100% 누락 방지는 불가능하지만, 넓은 키워드와 다중 사이트 확인으로 누락 가능성을 줄이는 구조입니다.
