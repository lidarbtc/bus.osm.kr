# bus.osm.kr

서울·경기 버스 공공데이터와 OpenStreetMap을 비교하는 도구와 운영 대시보드입니다. 개인 API 키 없이 공개 파일만 씁니다.

대시보드: https://bus.osm.kr

## 로컬 실행

Python 3.12와 [uv](https://docs.astral.sh/uv/)가 필요합니다.

```bash
uv sync --locked
uv run --locked bus-sync run --data-dir data --output-dir reports --exceptions config/exceptions.json
```

공식 파일 6개와 한국 OSM PBF(약 275MB)를 받아 비교하고 `reports/`에 보고서(`report.html`), 전체 결과(`comparison.json`, `stops.csv`, `routes.csv`, `stops.geojson`), 원천 기록(`*-manifest.json`)을 만듭니다.

## 원천

- **서울**: 열린데이터광장 [정류소 위치](https://data.seoul.go.kr/dataList/OA-15067/A/1/datasetView.do), [노선 기본정보](https://data.seoul.go.kr/dataList/OA-15066/F/1/datasetView.do), [노선별 경유정류소](https://data.seoul.go.kr/dataList/OA-1095/S/1/datasetView.do)
- **경기**: [GBIS 공개 페이지](https://www.gbis.go.kr/gbis2014/publicService.action?cmd=tBaseInfo)가 쓰는 요청으로 최신 파일을 찾습니다.
- **OSM**: [Geofabrik 한국 추출본](https://download.geofabrik.de/asia/south-korea.html). 서울(`KR-11`)·경기(`KR-41`) 행정경계도 같은 파일에서 만듭니다.

## 상태

| 상태 | 의미 |
|---|---|
| `matched` | 공식 ID·번호, 이름, 위치가 대응 |
| `matched_metadata` | 노선번호·운영사·정류소 순서가 대응 |
| `candidate_match` | 검토할 후보 |
| `osm_unmatched` | 공식 대응이 없는 OSM 객체. 보존 |
| `source_unmatched` | OSM 대응이 없는 공식 레코드 |
| `preserved_exception` | 예외 목록에 있는 OSM 객체 |
| `needs_classification`, `needs_scope_review` | 교통수단 또는 지역 확인 필요 |
| `outside_scope_support`, `out_of_mode_scope` | 경계 밖 연결용 객체, 버스 외 교통수단 |

`config/exceptions.json`에는 공공데이터에 없지만 유효한 OSM 객체(대학 셔틀, 놀이공원 정류소 등)를 근거, 재검토일과 함께 적습니다. 재검토일이 지나면 `review_due`로 표시합니다.

## 정류소 번호 추가 계획

비교할 때마다 번호(`ref`)가 없는 OSM 정류소에 붙일 공식 정류소 번호를 계획합니다.

- 서울과 경기가 함께 싣는 정류소는 번호나 진행 방향으로 하나로 합칩니다. 번호가 둘이면 정류소가 있는 지역의 번호를 씁니다.
- 같은 이름의 정류소는 묶어서 짝을 정합니다. 이미 번호가 있는 정류소는 고정하고, 길 건너편은 공식 노선의 정차 순서로 구한 진행 방향과 우측통행으로 구분합니다.
- 거리, 길의 쪽, 이름이 다른 이웃 정류소가 모두 한 답을 가리킬 때만 계획에 넣고, 나머지는 매퍼 확인 작업(`ref-tasks.geojson`, MapRoulette용)으로 남깁니다.
- 매 실행마다 번호가 있는 OSM 정류소 절반의 번호를 가리고 다시 계획해 기존 번호와의 일치율을 잽니다.

결과는 `ref-plan.json`(변경과 근거), `ref-changes.osc`(JOSM용)에 있습니다. 구·시별 파일은 `bus-sync export-osc --district 종로구 --output 종로구.osc`로 만듭니다.

## 운영

| 구성 | 위치 |
|---|---|
| 비교 실행기 | `oracle-kr-2`의 `/srv/osm-korea-bus-sync`. 매일 04:17 KST 비교, 15분마다 롤백 요청 확인 |
| 대시보드 | Vercel 프로젝트 `bus-osm-kr`의 `dashboard/`. 상태와 보고서는 비공개 Vercel Blob에 저장 |
| CI | `main`에 push하면 GitHub Actions가 테스트하고, Vercel이 대시보드를 배포 |

실행기는 매번 대시보드의 정지 스위치를 먼저 읽고, 정지 상태이거나 대시보드에 닿지 못하면 실행하지 않습니다. 서버는 5분마다 테스트를 통과한 최신 커밋을 받아 설치합니다.

비상 정지와 롤백은 한국에서 변경세트를 10개 이상 올렸고 활성 차단이 없는 OSM 계정이면 요청할 수 있고, 재개는 `ADMIN_OSM_UIDS`의 운영자가 합니다. 모든 조작은 계정 이름, 사유와 함께 공개 기록됩니다. 롤백은 봇 변경세트를 되돌리며, 그 뒤 다른 사람이 고친 객체가 있으면 수동 처리로 넘깁니다.

자동 편집 계정은 [lidarbtc_import](https://www.openstreetmap.org/user/lidarbtc_import)입니다.

로그인은 OSM OAuth 2.0 앱(Confidential, 권한 `read_prefs`, 리디렉션 URI `https://bus.osm.kr/api/auth/callback`)을 씁니다.

| Vercel 환경 변수 | 용도 |
|---|---|
| `OSM_CLIENT_ID`, `OSM_CLIENT_SECRET` | OSM 로그인 |
| `ADMIN_OSM_UIDS` | 재개 권한이 있는 OSM 사용자 번호(쉼표 구분) |
| `RUNNER_TOKEN` | 실행기 인증. 서버 `/etc/osm-korea-bus-sync/runner.env`와 같은 값 |
| `SESSION_SECRET` | 로그인 쿠키 서명 |
| `PUBLIC_ORIGIN` | `https://bus.osm.kr` |
| `BLOB_READ_WRITE_TOKEN` | Blob 연결 시 자동 추가 |

서버 상태는 `systemctl list-timers 'osm-korea-bus-sync*'`, 로그는 `journalctl -u osm-korea-bus-sync.service`로 봅니다. 최초 설정은 `deploy/server/setup.sh`에 있습니다.

## 테스트

```bash
uv run --locked pytest -q
cd dashboard && npm ci && npm test
```

## 라이선스

코드는 Apache-2.0입니다. OSM 데이터는 [© OpenStreetMap contributors, ODbL](https://www.openstreetmap.org/copyright), 서울 파일은 공공누리 1유형이며, 경기 데이터의 이용조건은 manifest에 기록합니다.
