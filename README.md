# bus.osm.kr

서울·경기 버스 공공데이터와 OpenStreetMap을 비교하는 도구와 운영 대시보드입니다. 개인 API 키 없이 공개 파일만 씁니다. 지금은 **읽기 전용**이며 OSM 업로드는 비활성화되어 있습니다.

대시보드: https://bus.osm.kr

## 로컬 실행

Python 3.12와 [uv](https://docs.astral.sh/uv/)가 필요합니다.

```bash
uv sync --locked
uv run --locked bus-sync run --data-dir data --output-dir reports --exceptions config/exceptions.json
```

공식 파일 6개와 한국 OSM PBF(약 275MB)를 받아 비교하고, `reports/report.html`에 검색·필터·지도가 있는 보고서를 만듭니다. 전체 결과는 `comparison.json`, `stops.csv`, `routes.csv`, `stops.geojson`에, 원천 URL·기준일·해시는 `*-manifest.json`에 남습니다. 실패하면 종료 코드 1과 함께 이전 성공 보고서를 실패 보고서로 바꿉니다.

## 원천

- **서울**: 열린데이터광장 [정류소 위치](https://data.seoul.go.kr/dataList/OA-15067/A/1/datasetView.do), [노선 기본정보](https://data.seoul.go.kr/dataList/OA-15066/F/1/datasetView.do), [노선별 경유정류소](https://data.seoul.go.kr/dataList/OA-1095/S/1/datasetView.do)
- **경기**: [GBIS 공개 페이지](https://www.gbis.go.kr/gbis2014/publicService.action?cmd=tBaseInfo)가 쓰는 내부 요청으로 최신 파일을 찾습니다. 정식 API 계약이 아니라서 바뀌면 추측하지 않고 실패합니다.
- **OSM**: [Geofabrik 한국 추출본](https://download.geofabrik.de/asia/south-korea.html). 서울(`KR-11`)·경기(`KR-41`) 행정경계도 같은 파일에서 만듭니다.

## 결과 읽는 법

집계 단위는 공급원 레코드와 OSM 객체입니다. 물리 정류장이나 운행 계통 수가 아닙니다.

| 상태 | 의미 |
|---|---|
| `matched` | 공공 ID·번호와 이름, 위치가 함께 대응 |
| `matched_metadata` | 노선번호·운영사·정류소 순서가 대응. 경로 형상은 검증하지 않음 |
| `candidate_match` | 대응 가능성이 있어 검토 필요 |
| `osm_unmatched` | 공공 대응이 없는 OSM 객체. **삭제 대상이 아니며 보존** |
| `source_unmatched` | OSM 대응이 없는 공식 레코드. 신규 생성 판정이 아님 |
| `preserved_exception` | 예외 목록의 보존 대상 |
| `needs_classification`, `needs_scope_review` | 교통수단 또는 지역 판정이 불확실 |
| `outside_scope_support`, `out_of_mode_scope` | 경계 밖 연결용 객체, 버스 외 교통수단 |

`config/exceptions.json`에는 공공데이터에 없지만 유효한 OSM 객체(대학 셔틀, 놀이공원 정류소 등)를 근거와 재검토일과 함께 적습니다. 예외는 보존만 하며, 재검토일이 지나도 지우지 않고 `review_due`로 표시합니다.

## 운영

| 구성 | 위치 |
|---|---|
| 비교 실행기 | `oracle-kr-2`의 `/srv/osm-korea-bus-sync`. 매일 04:17 KST 비교, 15분마다 롤백 요청 확인 |
| 대시보드 | Vercel 프로젝트 `bus-osm-kr`의 `dashboard/`. 상태와 보고서는 비공개 Vercel Blob에 저장 |
| CI | `main`에 push하면 GitHub Actions가 테스트하고, Vercel이 대시보드를 배포 |

실행기는 매번 대시보드의 정지 스위치를 먼저 읽고, 정지 상태이거나 대시보드에 닿지 못하면 실행하지 않습니다. 서버는 5분마다 테스트가 통과한 최신 커밋을 직접 받아 설치하므로 GitHub에 서버 접속 키가 없습니다.

**비상 정지·롤백**은 한국에서 변경세트를 10개 이상 올렸고 활성 차단이 없는 OSM 계정이면 누구나 요청할 수 있습니다. 재개는 `ADMIN_OSM_UIDS`에 등록된 운영자만 할 수 있습니다. 모든 조작은 계정 이름과 사유가 공개 기록됩니다. 롤백은 이 시스템의 봇이 만든 변경세트만 대상으로 하며, 그 뒤 다른 사람이 같은 객체를 고쳤다면 아무것도 올리지 않고 수동 처리로 넘깁니다.

로그인에는 OSM OAuth 2.0 앱이 필요합니다. 등록 조건은 Confidential, 권한 `read_prefs`, 리디렉션 URI `https://bus.osm.kr/api/auth/callback`입니다.

| Vercel 환경 변수 | 용도 |
|---|---|
| `OSM_CLIENT_ID`, `OSM_CLIENT_SECRET` | OSM 로그인 |
| `ADMIN_OSM_UIDS` | 재개 권한이 있는 OSM 사용자 번호(쉼표 구분) |
| `RUNNER_TOKEN` | 실행기 인증. 서버 `/etc/osm-korea-bus-sync/runner.env`와 같은 값 |
| `SESSION_SECRET` | 로그인 쿠키 서명 |
| `PUBLIC_ORIGIN` | `https://bus.osm.kr` |
| `BLOB_READ_WRITE_TOKEN` | Blob 연결 시 자동 추가 |

서버 상태는 `systemctl list-timers 'osm-korea-bus-sync*'`, 로그는 `journalctl -u osm-korea-bus-sync.service`로 봅니다. 최초 설정은 `deploy/server/setup.sh`를 참고하세요.

## 테스트

```bash
uv run --locked pytest -q
cd dashboard && npm ci && npm test
```

## 라이선스

코드는 Apache-2.0입니다. OSM 데이터는 [© OpenStreetMap contributors, ODbL](https://www.openstreetmap.org/copyright), 서울 파일은 공공누리 1유형이며, 경기 데이터의 이용조건은 manifest에 기록합니다.
