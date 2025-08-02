use actix_web::{get, web, HttpResponse, Responder};
use deadpool_postgres::Pool;
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize)]
struct GGDBusStop {
    station_id: i32,
    station_nm: String,        // 필드명도 DB 컬럼명과 일치시키는 것이 좋음
    center_id: Option<String>, // NULL 값을 받을 수 있도록 Option<String>으로 변경
    center_yn: String,
    x: f64,
    y: f64,
    region_name: String,
    mobile_no: Option<String>,
    district_cd: Option<String>, // NULL 값을 받을 수 있도록 Option<String>으로 변경
}

#[derive(Deserialize)]
struct BboxQuery {
    bbox: String,
}

#[get("/api/ggd/bus_stops")]
pub async fn get_ggd_bus_stops(
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

    // SQL 쿼리의 컬럼명을 실제 DB에 저장된 소문자 이름으로 변경
    let stmt = match client
        .prepare(
            "SELECT station_id, station_nm, center_id, center_yn, x, y, region_name, mobile_no, district_cd
             FROM ggd_bus_stops
             WHERE x BETWEEN $1 AND $2 AND y BETWEEN $3 AND $4",
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
            let mut bus_stops = Vec::<GGDBusStop>::new();

            for row in rows {
                // row.get()의 컬럼명도 소문자로 변경
                let bus_stop = GGDBusStop {
                    station_id: row.get("station_id"),
                    station_nm: row.get("station_nm"),
                    center_id: row.get("center_id"),
                    center_yn: row.get("center_yn"),
                    x: row.get("x"),
                    y: row.get("y"),
                    region_name: row.get("region_name"),
                    mobile_no: row.get("mobile_no"),
                    district_cd: row.get("district_cd"),
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
