use actix_web::{get, web, HttpResponse, Responder};
use deadpool_postgres::Pool;
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize)]
struct GGDBusStop {
    station_id: i32,
    station_name: String,
    center_id: String,
    center_yn: String,
    x: f64,
    y: f64,
    region_name: String,
    mobile_no: String,
    district_cd: String,
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
    let client = db.get().await.unwrap();
    let bbox: Vec<&str> = query.bbox.split(',').collect();

    if bbox.len() != 4 {
        return HttpResponse::BadRequest().body("Invalid bbox parameter");
    }

    let min_lng: f64 = bbox[0].parse().unwrap();
    let min_lat: f64 = bbox[1].parse().unwrap();
    let max_lng: f64 = bbox[2].parse().unwrap();
    let max_lat: f64 = bbox[3].parse().unwrap();

    let stmt = client
        .prepare(
            "SELECT STATION_ID, STATION_NM, CENTER_ID, CENTER_YN, X, Y, REGION_NAME, MOBILE_NO, DISTRICT_CD 
             FROM ggd_bus_stops
             WHERE X BETWEEN $1 AND $2 AND Y BETWEEN $3 AND $4",
        )
        .await
        .unwrap();

    let bus_stops_result = client
        .query(&stmt, &[&min_lng, &max_lng, &min_lat, &max_lat])
        .await;

    match bus_stops_result {
        Ok(rows) => {
            let mut bus_stops = Vec::<GGDBusStop>::new();

            for row in rows {
                let bus_stop = GGDBusStop {
                    station_id: row.get("STATION_ID"),
                    station_name: row.get("STATION_NM"),
                    center_id: row.get("CENTER_ID"),
                    center_yn: row.get("CENTER_YN"),
                    x: row.get("X"),
                    y: row.get("Y"),
                    region_name: row.get("REGION_NAME"),
                    mobile_no: row.get("MOBILE_NO"),
                    district_cd: row.get("DISTRICT_CD"),
                };

                bus_stops.push(bus_stop);
            }

            HttpResponse::Ok().json(bus_stops)
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}
