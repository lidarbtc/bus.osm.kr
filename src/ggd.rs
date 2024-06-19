use actix_web::{get, web, HttpResponse, Responder};
use deadpool_postgres::Pool;
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize)]
struct GGDBusStop {
    city_name: String,
    stop_name: String,
    stop_english_name: String,
    stop_id: i32,
    stop_number: f64,
    central_lane: String,
    jurisdiction: String,
    location: String,
    latitude: f64,
    longitude: f64,
}

#[get("/api/ggd/bus_stops")]
pub async fn get_ggd_bus_stops(db: web::Data<Pool>) -> impl Responder {
    let client = db.get().await.unwrap();

    let stmt = client
        .prepare("SELECT 시군명, 정류소명, 정류소영문명, 정류소id, 정류소번호, 중앙차로여부, 관할관청, 위치, WGS84위도, WGS84경도 FROM ggd_bus_stops")
        .await
        .unwrap();

    let bus_stops_result = client.query(&stmt, &[]).await;

    match bus_stops_result {
        Ok(rows) => {
            let mut bus_stops = Vec::<GGDBusStop>::new();

            for row in rows {
                let bus_stop = GGDBusStop {
                    city_name: row.get("시군명"),
                    stop_name: row.get("정류소명"),
                    stop_english_name: row.get("정류소영문명"),
                    stop_id: row.get("정류소id"),
                    stop_number: row.get("정류소번호"),
                    central_lane: row.get("중앙차로여부"),
                    jurisdiction: row.get("관할관청"),
                    location: row.get("위치"),
                    latitude: row.get("WGS84위도"),
                    longitude: row.get("WGS84경도"),
                };

                bus_stops.push(bus_stop);
            }

            HttpResponse::Ok().json(bus_stops)
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}
