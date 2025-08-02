use actix_web::{get, web, HttpResponse, Responder};
use deadpool_postgres::Pool;
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize)]
struct BusStop {
    stop_id: i32,
    name: String,
    stop_type: String,
    stop_number: String,
    latitude: f64,
    longitude: f64,
    info_display: Option<String>, // NULL 값을 받을 수 있도록 Option<String>으로 변경
}

#[derive(Deserialize)]
struct BboxQuery {
    bbox: String,
}

#[get("/api/seoul/bus_stops")]
pub async fn get_seoul_bus_stops(
    db: web::Data<Pool>,
    query: web::Query<BboxQuery>,
) -> impl Responder {
    let client = match db.get().await {
        Ok(client) => client,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let bbox: Vec<&str> = query.bbox.split(',').collect();

    if bbox.len() != 4 {
        return HttpResponse::BadRequest().body("Invalid bbox parameter");
    }

    let min_lng: f64 = bbox[0].parse().unwrap();
    let min_lat: f64 = bbox[1].parse().unwrap();
    let max_lng: f64 = bbox[2].parse().unwrap();
    let max_lat: f64 = bbox[3].parse().unwrap();

    // SQL 쿼리의 컬럼명을 실제 DB에 저장된 소문자 이름으로 변경 (예: 정류장_ID -> 정류장_id)
    let stmt = match client
        .prepare(
            "SELECT 정류장_id, 정류장_명칭, 정류장_유형, 정류장_번호, 위도, 경도, 버스도착정보안내기_설치_여부 FROM seoul_bus_stops
                  WHERE 경도 BETWEEN $1 AND $2 AND 위도 BETWEEN $3 AND $4",
        )
        .await {
            Ok(stmt) => stmt,
            Err(_) => return HttpResponse::InternalServerError().finish(),
        };

    let bus_stops_result = client
        .query(&stmt, &[&min_lng, &max_lng, &min_lat, &max_lat])
        .await;

    match bus_stops_result {
        Ok(rows) => {
            let mut bus_stops = Vec::<BusStop>::new();

            for row in rows {
                // row.get()의 컬럼명도 소문자로 변경
                let bus_stop = BusStop {
                    stop_id: row.get("정류장_id"),
                    name: row.get("정류장_명칭"),
                    stop_type: row.get("정류장_유형"),
                    stop_number: row.get("정류장_번호"),
                    latitude: row.get("위도"),
                    longitude: row.get("경도"),
                    info_display: row.get("버스도착정보안내기_설치_여부"),
                };

                bus_stops.push(bus_stop);
            }

            HttpResponse::Ok().json(bus_stops)
        }
        Err(e) => {
            eprintln!("Database query error: {}", e);
            HttpResponse::InternalServerError().finish()
        }
    }
}
