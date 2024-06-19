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

#[get("/api/seoul/bus_stops")]
pub async fn get_seoul_bus_stops(db: web::Data<Pool>) -> impl Responder {
    let client = db.get().await.unwrap();

    let stmt = client
        .prepare("SELECT NODE_ID, ARS_ID, 정류소명, X좌표, Y좌표, 정류소타입 FROM seoul_bus_stops")
        .await
        .unwrap();

    let bus_stops_result = client.query(&stmt, &[]).await;

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
