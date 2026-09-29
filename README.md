# Korea Trade Monitor (Python / FastAPI)

관세청 「품목별 국가별 수출입실적」·「수출입총괄」 OpenAPI를 연동한 한국 수출입 트래킹 대시보드입니다.
국가 전체 수출입, 섹터별(반도체·전력기기·자동차 등) 월별 추이, **국가·HS코드·품목명 검색 + 엑셀(.xlsx) 다운로드**를 제공합니다.

## 1. 내 PC / Antigravity 에서 실행
1. Python 3.10+ 설치 확인: `python --version` (Windows는 `py --version`)
2. Antigravity(또는 VS Code 등)에서 이 폴더(`trade-dashboard-py`)를 열고 터미널을 엽니다.
3. 가상환경 + 패키지 설치
   - Windows: `python -m venv .venv` → `.venv\Scripts\activate` → `pip install -r requirements.txt`
   - Mac/Linux: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
4. `.env.example` 을 복사해 `.env` 로 만들고 `CUSTOMS_API_KEY=` 뒤에 발급받은 인증키 입력
   (Windows: `copy .env.example .env` / Mac·Linux: `cp .env.example .env`)
5. 실행: `python main.py` → 브라우저에서 http://127.0.0.1:8000 (첫 로딩은 데이터 수집으로 수십 초 걸릴 수 있음)
키가 없으면 가상 샘플 데이터로 화면만 확인할 수 있습니다(화면에 경고 표시).

## 2. 누구나 URL로 접속하게 배포하기 (예: Render 무료 플랜)
1. GitHub에 이 폴더를 올립니다 (`.env` 는 `.gitignore` 로 제외되어 키가 올라가지 않음).
2. https://render.com → New → **Blueprint**(또는 Web Service) → 저장소 선택 (`render.yaml` 자동 인식)
3. 환경변수 `CUSTOMS_API_KEY` 에 인증키 입력 → Deploy
4. 발급된 `https://xxxx.onrender.com` 주소만 공유하면 누구나 접속할 수 있습니다.
- Railway / Fly.io / Google Cloud Run 은 `Dockerfile` 로 배포 (`PORT` 는 플랫폼이 주입, `TRUST_PROXY=1`).
- 무료 플랜은 접속이 없으면 잠들어 첫 접속이 느릴 수 있습니다. 발표 전에 미리 한 번 열어 두세요.
- 헬스체크: `GET /healthz`

## 3. 구조
```
main.py        FastAPI 서버: API 프록시·캐시·검색·엑셀 생성·보안 헤더·요청 제한
config.py      섹터/HS코드, API 주소, 검색 제한, 국가 목록
public/        index.html, app.js(SVG 차트/UI), style.css (외부 라이브러리 없음)
```
엔드포인트: `/api/data`, `/api/meta`, `/api/search`, `/api/download.xlsx`, `/healthz`

## 4. 관세청 API (실제 호출로 확인)
- 품목·국가별: `https://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList` — `strtYymm`, `endYymm`, `hsSgn`(2·4·6·10자리), `cntyCd`(ISO 2자리); 품목/국가 중 1개 이상 필수
- 국가 전체: `https://apis.data.go.kr/1220000/Newtrade/getNewtradeList` — 월별 `expDlr`, `impDlr`, `balPayments`
- 조회기간 1년 이내 제한 → 12개월 단위로 분할 호출. 응답은 XML, 금액 단위 USD

## 5. 보안 (Secure Coding)
- API 키는 환경변수로만 사용, 브라우저·로그에 노출하지 않음(오류 메시지 마스킹), `.env` 는 git 제외
- 검색 입력 화이트리스트 검증(HS 숫자 2·4·6·10자리, 국가 `[A-Z]{2}`, 연월 정규식, 개수·기간·행수 상한)
- XML은 정규식 파서로 처리(XXE·엔티티 폭탄 없음), 엑셀 셀은 문자열 타입으로 저장(수식 인젝션 차단)
- CSP·X-Frame-Options 등 보안 헤더, GET 전용, 정적 파일 화이트리스트, API 문서 페이지 비활성화
- IP당 요청 제한(전체 분당 120, 검색·다운로드 분당 20), 동시 검색 2건 제한, 결과 30만 행 상한

## 6. 성능
- 결과 6시간 캐시 + stale-while-revalidate(만료돼도 지연 없이 응답) + 기동 시 사전 로드
- 비동기 HTTP(httpx) + 동시 호출 4개 제한, 대용량 XML 파싱은 스레드에서 처리(이벤트 루프 비차단)
- 엑셀은 openpyxl write-only 모드로 생성(메모리 절약), 캐시는 단일 항목이라 메모리 증가 없음

## 7. 한계 / 유의
- "실시간"은 관세청 월 통계의 자동 갱신입니다(체결 단위 아님). 최신월은 잠정 집계이며 이후 수정될 수 있습니다.
- 섹터는 대표 HS코드 기준 근사치입니다(`config.py` 수정 가능). 증감률은 전년 동월 수출액이 100만 달러 미만이면 표시하지 않습니다.
- 공개 배포 시 접속자가 늘면 공공데이터포털 API 일일 호출 한도에 걸릴 수 있으므로 캐시를 유지하세요(대시보드는 캐시로 호출이 거의 없음, 검색은 호출 발생).

데이터 출처: 관세청 (공공데이터포털 data.go.kr)
