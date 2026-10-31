# 서울·경기 버스와 OSM 비교

서울·경기 공개 파일과 한국 OSM 추출본을 비교하는 도구입니다. 개인 API 키 없이 실행합니다. 비교는 읽기 전용이며 **OSM 업로드는 아직 비활성화**되어 있습니다. 운영 대시보드: https://bus.osm.kr

## 실행

Python 3.12와 [uv](https://docs.astral.sh/uv/)를 사용합니다.

```bash
uv sync --locked
uv run --locked bus-sync run --data-dir data --output-dir reports --exceptions config/exceptions.json
```

이 명령은 최신 공식 파일 목록을 확인하고 파일 6개와 한국 OSM PBF를 내려받은 뒤, 서울·경기 전체를 비교합니다. OSM 파일은 현재 약 275MB이며, 최초 실행에는 다운로드 시간이 필요합니다. 개인 API 키나 OSM 계정은 입력하지 않습니다.

결과는 `reports/report.html`을 브라우저에서 열어 확인합니다. 지도·검색·상태 필터·후보별 근거가 포함되며, 외부 지도 타일이나 스크립트를 요청하지 않습니다.

| 파일 | 내용 |
|---|---|
| `report.html` | 검색·필터와 행정경계/정류소 위치 지도 |
| `comparison.json` | 전체 비교 결과, 근거, 예외, 경고 |
| `stops.csv`, `routes.csv` | 공급원 레코드와 OSM 객체별 대응 후보 |
| `stops.geojson` | 위치가 있는 정류소 결과 전체 |
| `summary.md`, `status.json` | 실행 상태와 집계 |
| `source-manifest.json`, `osm-manifest.json` | 원천 URL·기준일·해시·취득 경로 |

실패하면 명령이 1을 반환하고 실패 보고서를 생성합니다. 같은 출력 폴더의 이전 성공 보고서는 실패 상태로 교체하여, 오래된 결과를 최신 결과로 오인하지 않도록 합니다. 원천의 마지막 정상 캐시는 유지하지만 자동으로 대신 사용하지 않습니다.

수집과 오프라인 비교를 나누어 실행할 수도 있습니다.

```bash
uv run --locked bus-sync fetch --data-dir data
uv run --locked bus-sync fetch-osm --data-dir data
uv run --locked bus-sync compare --data-dir data --osm-file data/raw/south-korea-YYMMDD.osm.pbf --output-dir reports
```

마지막 명령의 파일명은 실제 내려받은 파일로 바꿉니다. `compare`는 명시적으로 저장된 스냅샷을 사용하며 최신 수집을 뜻하지 않습니다. 외부에서 받은 OSM 파일도 읽을 수 있지만, 행정경계와 참조 무결성을 확인하는 것만으로 파일의 전수성을 보장하지는 않습니다. Geofabrik 다운로드 원장과 SHA256이 일치할 때 국가 전체 추출본 출처를 표시합니다.

## 원천과 범위

- 서울: [정류소 위치](https://data.seoul.go.kr/dataList/OA-15067/A/1/datasetView.do), [노선 기본정보](https://data.seoul.go.kr/dataList/OA-15066/F/1/datasetView.do), [노선별 경유정류소](https://data.seoul.go.kr/dataList/OA-1095/S/1/datasetView.do). 각 페이지에서 파일 날짜와 다운로드 폼을 읽습니다. 날짜·첨부파일 번호를 코드에 고정하지 않습니다.
- 경기: [공식 공개 테스트 페이지](https://www.gbis.go.kr/gbis2014/publicService.action?cmd=tBaseInfo)의 공개 테스트 요청으로 최신 버전과 파일 URL을 알아낸 뒤 전체 정류소·노선·경유정보를 받습니다. **개인 키가 필요 없지만 사이트 내부 API 호출은 있습니다.** 이 경로는 장기 안정성이 보장된 정식 API 계약이 아닙니다. 중단되면 명시적으로 실패하며 날짜를 추측하거나 오래된 파일로 대체하지 않습니다.
- OSM: [Geofabrik 한국 전체 추출본](https://download.geofabrik.de/asia/south-korea.html)의 날짜별 PBF와 제공자 MD5를 검증하고 SHA256도 기록합니다. 서울 `KR-11`, 경기 `KR-41` 행정경계를 같은 파일에서 구성합니다. 경계가 빠지거나 깨지면 사각형으로 대체하지 않고 실패합니다.

정류소의 지역 범위와 노선의 인허가 범위는 다릅니다. 공식 노선은 서울·경기 공급원 전체를 비교하며, 공식 정류소 결과의 주 집계는 두 행정경계 안입니다. OSM은 두 지역을 지나는 버스 노선과 관련 `route_master` 전체 members, 교차노선의 지역 밖 정류소도 유지합니다. 경계 밖 보조 객체와 위치 미확정 객체는 지역 내 집계와 분리합니다.

서울 정류소 파일에 없는 외곽 정류소도 경유정보에 이름·좌표가 일관되게 있으면 `origin=route_stations`로 보충합니다. 원본 정류소와 보충 정류소 개수를 따로 기록합니다. 관광노선의 경유정보 누락, 한강선착장, 유형 미상, 경기 DRT, 중복 정류소 번호를 별도로 표시합니다. 경기 `mobileNo`를 고유키로 사용하지 않습니다.

공식 파일 6개는 매번 다시 확인하여 같은 날짜·URL에서 수정된 내용도 감지합니다. 파일 파싱이 모두 성공한 뒤에만 새 원천 manifest를 공개합니다. 파일별 기준일과 취득 시각을 분리하며 서로 다른 날짜의 파일을 합친 경우 경고합니다.

## 비교 결과 해석

| 상태 | 의미 |
|---|---|
| `matched` | 가까운 위치의 공공 ID/번호와 이름 또는 매우 가까운 위치가 함께 대응함 |
| `matched_metadata` | 노선번호와 운영사 또는 정류소 순서 근거가 대응함. 경로 검증을 뜻하지 않음 |
| `candidate_match` | 대응 가능성이 있지만 검토가 필요함 |
| `osm_unmatched` | 이 입력에서 공공 대응을 찾지 못한 OSM 객체. 기본 보존 |
| `source_unmatched` | 이 입력에서 OSM 대응을 찾지 못한 공식 레코드. 신규 생성 판정이 아님 |
| `preserved_exception` | 예외 목록의 보존 대상 |
| `needs_classification` | 교통수단 분류가 불확실함 |
| `needs_scope_review` | 위치·참조 오류로 지역 판정이 불확실함 |
| `outside_scope_support` | 전체 노선 관계를 보존하기 위한 경계 밖 객체 |
| `out_of_mode_scope` | 수상교통 등 버스 비교 밖의 교통수단 |

정류소는 250m 이내 후보를 찾되 이름·거리만으로 확정하지 않습니다. ID/번호 근거가 있어도 100m를 넘으면 검토 대상으로 남기고, 한 공급원에서 여러 정류소가 강하게 대응하면 모호한 후보로 처리합니다. 공급원별 `ref:*`를 섞지 않으며 `local_ref`는 정류소 ID로 사용하지 않습니다. 플랫폼과 도로 위 정차 지점은 같은 공식 레코드에 대응할 수 있습니다.

노선번호의 하이픈을 유지하여 `10-1`과 `101`을 구분합니다. 동일 번호가 여러 운영사·지역에 있으면 별도 후보로 남깁니다. 양방향·분기 relation과 `route_master`는 구분하여 집계합니다. 노선의 OSM 도로 way 형상·연결성·시간표를 정확하다고 인증하는 도구는 아닙니다. 경기 형상 파일 URL은 기록하지만 이 버전에서는 내려받거나 경로를 수정하지 않습니다.

모든 집계는 **공급원 레코드 / OSM 객체 단위**입니다. 서울·경기의 같은 정류소 레코드, 플랫폼·정차 지점, 양방향 노선 관계를 물리 정류장이나 운행 계통 개수로 합산하지 않습니다.

## 예외 목록

`config/exceptions.json`에 확인 근거와 검토일을 기록합니다. 초기 목록은 연세대 셔틀 6개 노선 관계와 서울대·에버랜드 정류소 후보 11개 객체를 포함합니다. 서비스 존재를 확인한 것과 각 승차홈의 위치·용도를 확정한 것을 구분합니다.

- 예외가 없는 미일치 OSM 객체도 기본 보존합니다.
- 재검토 날짜가 지나면 `review_due=true`가 되며 보존은 유지됩니다.
- 이번 추출본에서 예외 객체가 안 보이면 `not_observed`로 표시합니다.
- 예외 노선의 모든 정류소를 일괄 제외하지 않습니다. 일반 버스와 공유할 수 있기 때문입니다.

## 운영 구조

| 구성 | 위치 | 역할 |
|---|---|---|
| 비교 실행기 | `oracle-kr-2` (`/srv/osm-korea-bus-sync`, 사용자 `osmbus`) | 매일 **04:17 KST** 수집·비교, 15분마다 롤백 요청 처리 |
| 대시보드 | [bus.osm.kr](https://bus.osm.kr) (Vercel 프로젝트 `bus-osm-kr`, `dashboard/`, 함수 리전 icn1, 비공개 Blob 저장소) | 실행 결과·보고서, 비상 정지, 롤백 요청, 공개 감사 기록 |
| CI/CD | GitHub Actions, Vercel Git 연동 | Actions는 테스트, Vercel은 `dashboard/` 변경 시 배포. 서버는 테스트가 성공한 커밋을 스스로 가져와 설치 |

실행기는 매번 대시보드의 정지 스위치를 먼저 읽습니다. **정지 상태이거나 대시보드에 닿지 못하면 비교를 실행하지 않습니다.** 롤백 요청은 정지 중에도 처리합니다.

대시보드 상태(정지 스위치, 실행 기록, 봇 변경세트, 롤백 작업, 감사 기록)는 Blob의 JSON 파일 하나에 저장하고, ETag 조건부 쓰기로 동시 수정을 막습니다. 보고서 파일은 실행기가 gzip으로 압축해 대시보드가 발급한 30분짜리 서명 URL로 Blob에 직접 올립니다. Vercel 함수 응답은 4.5MB로 제한되므로, `/api/reports/<실행 ID|latest>/<파일>`은 10분짜리 서명 다운로드 주소로 넘겨 줍니다. `/report.html?run=latest`는 `report-data.json`을 받아 브라우저에서 풀고 `textContent`로만 표시합니다. 최근 20개 실행의 파일만 보관합니다(`RETAIN_RUN_FILES`).

Hobby 플랜 Blob 한도(저장 1GB, 월 전송 10GB, 월 단순 작업 1만·고급 작업 2천 회)에 맞춰 실행기 heartbeat는 3시간에 한 번만 쓰고, 롤백 확인 주기는 15분으로 둡니다. 2026-10-31 첫 실행 기준으로 압축 보고서는 1회 약 25MB이므로, 20회를 보관하면 약 500MB를 씁니다.

### 비상 정지와 롤백 권한

OSM 계정으로 로그인합니다(OAuth 2.0, `read_prefs`만 요청하고 토큰은 신원 확인 직후 폐기). 다음 조건을 모두 만족하는 계정이 비상 정지와 롤백을 요청할 수 있습니다.

- 한국에서 올린 변경세트가 **10개 이상**: 닫힌 변경세트 중 실제 변경이 1건 이상이고, 변경 범위의 중심점이 대한민국 OSM 국가 경계(relation 307756, 영해 포함·단순화) 안인 것만 셉니다. 최근 500개까지 확인합니다.
- 현재 활성 차단(block)이 없음

재개는 `ADMIN_OSM_UIDS`(Vercel 환경 변수, 쉼표로 구분한 OSM 사용자 번호)에 등록된 운영자만 할 수 있습니다. 운영자 여부는 요청마다 이 값으로 다시 판단하므로 목록에서 빼면 바로 권한이 사라집니다. 모든 정지·재개·롤백은 계정 이름과 사유가 대시보드에 공개 기록됩니다. 세션은 서명된 쿠키로 12시간 유지하며, 요청마다 출처와 CSRF 토큰을 확인합니다.

OSM OAuth 2.0 앱은 Confidential, 권한 `read_prefs`, 리디렉션 URI `https://bus.osm.kr/api/auth/callback`으로 등록합니다.

롤백은 **이 시스템의 봇 계정이 만든 변경세트**(대시보드의 봇 변경세트 목록)만 대상으로 합니다. 요청하면 자동 작업이 즉시 정지되고, 서버가 15분 안에 처리합니다.

- 봇이 만든 객체는 삭제하고, 수정·삭제한 객체는 봇 이전 버전으로 복원합니다.
- 그 뒤 다른 사람이 같은 객체를 고쳤거나 OSM이 충돌을 반환하면 **아무것도 올리지 않고** `수동 처리 필요`로 표시합니다.
- 되돌리기는 변경세트 하나로 올리므로 OSM이 전부 반영하거나 전부 거부합니다.

현재는 **OSM 업로드가 비활성화**되어 봇 변경세트가 없고, 서버에 봇 계정 토큰(`OSM_BOT_TOKEN`, `OSM_BOT_UID`)도 없습니다. 롤백 경로는 테스트로만 검증했습니다.

### 배포

`main`에 push하면 `.github/workflows/deploy.yml`이 Python·대시보드 테스트를 실행합니다. 대시보드는 Vercel Git 연동이 `dashboard/`를 루트로 배포하며, 그 폴더에 변경이 없으면 빌드를 건너뜁니다. 서버는 Tailscale 안에 있어 GitHub에서 접속하지 않습니다. 대신 서버의 `osm-korea-bus-sync-deploy.timer`가 5분마다 이 workflow가 **성공한** 가장 최근 커밋을 확인하고, 그 커밋을 `releases/<sha>`에 설치한 뒤 `current`를 바꿉니다. 저장소가 공개라 서버에 GitHub 자격 증명이 없고, GitHub에도 서버 접속 키나 배포 토큰이 없습니다. 최근 3개 릴리스를 보관합니다.

| 비밀값 | 위치 |
|---|---|
| `RUNNER_TOKEN` | Vercel 환경 변수, 서버 `/etc/osm-korea-bus-sync/runner.env`의 `BUS_SYNC_RUNNER_TOKEN`(root:osmbus 0640) |
| `SESSION_SECRET` | Vercel 환경 변수. 로그인 쿠키 서명용, 바꾸면 모든 세션이 끝남 |
| `OSM_CLIENT_ID`, `OSM_CLIENT_SECRET` | Vercel 환경 변수 |
| `BLOB_READ_WRITE_TOKEN` | Vercel Blob 저장소 `osm-korea-bus-sync` 연결 시 자동 추가 |

그 밖의 Vercel 환경 변수는 `PUBLIC_ORIGIN=https://bus.osm.kr`, `ADMIN_OSM_UIDS`입니다. 대시보드 테스트는 `cd dashboard && npm ci && npm test`로 실행합니다.

서버 최초 설정은 저장소를 서버에 복사한 뒤 `sudo deploy/server/setup.sh`를 실행하고 표준 입력으로 실행기 토큰을 넣습니다. 상태 확인:

```bash
systemctl list-timers 'osm-korea-bus-sync*'
journalctl -u osm-korea-bus-sync.service -n 100
```

GitHub 실행기는 해외 IP라 한국 공공 사이트 접근이 불안정할 수 있어, 정기 비교는 한국 리전 서버에서 실행합니다. `test.yml`은 main 외 브랜치와 PR에서 같은 테스트를 실행하며, 외부 action은 검증한 릴리스 커밋 SHA로 고정되어 있습니다.

## 검증 및 현재 제약

```bash
uv run --locked pytest -q
```

테스트는 정상 입력뿐 아니라 공급원 장애·빈 응답·형식 변경·참조 충돌·잘린 경계·순환 관계·반대편 승강장·같은 노선번호·예외 만료·실패 후 오래된 보고서 노출을 확인합니다.

2026-10-10 개발 검증에서 서울 공개 파일과 한국 전체 OSM PBF를 실제로 파싱했습니다. 같은 시점에 GBIS 공개 사이트는 TLS 오류를, 과거 파일 다운로드는 빈 응답을 반환했습니다. 전체 서울·경기 수집이 완료됐다고 처리하지 않았으며, 이 장애가 계속되면 예정된 비교 workflow도 실패 상태를 보고합니다.

코드는 Apache-2.0입니다. 데이터의 이용조건은 별도입니다. OSM은 [© OpenStreetMap contributors / ODbL](https://www.openstreetmap.org/copyright), 서울 파일은 공공누리 1유형이며, 경기 데이터의 원천 이용조건과 URL은 manifest에 기록합니다. `data/`와 `reports/`는 Git에서 제외합니다.
