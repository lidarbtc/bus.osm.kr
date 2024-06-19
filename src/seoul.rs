use actix_web::{get, web, HttpResponse, Responder};
use deadpool_postgres::Pool;
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize)]
struct BusStop {
    node_id: i32,
    ars_id: String,
    name: String,
    x: f64,
    y: f64,
    stop_type: String,
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
            "SELECT NODE_ID, ARS_ID, 정류소명, X좌표, Y좌표, 정류소타입 FROM seoul_bus_stops 
                  WHERE X좌표 BETWEEN $1 AND $2 AND Y좌표 BETWEEN $3 AND $4",
        )
        .await
        .unwrap();

    let bus_stops_result = client
        .query(&stmt, &[&min_lng, &max_lng, &min_lat, &max_lat])
        .await;

    match bus_stops_result {
        Ok(rows) => {
            let mut bus_stops = Vec::<BusStop>::new();

            for row in rows {
                let bus_stop = BusStop {
                    node_id: row.get("NODE_ID"),
                    ars_id: row.get("ARS_ID"),
                    name: row.get("정류소명"),
                    x: row.get("X좌표"),
                    y: row.get("Y좌표"),
                    stop_type: row.get("정류소타입"),
                };

                bus_stops.push(bus_stop);
            }

            HttpResponse::Ok().json(bus_stops)
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}
