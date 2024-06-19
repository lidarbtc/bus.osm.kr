use actix_web::{web, App, HttpServer};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use dotenv::dotenv;
use num_cpus;
use std::env;
use tokio_postgres::NoTls;

mod seoul;
use seoul::get_seoul_bus_stops;

mod ggd;
use ggd::get_ggd_bus_stops;

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    dotenv().ok();
    let db_host = env::var("DB_HOST").unwrap();
    let db_user = env::var("DB_USER").unwrap();
    let db_password = env::var("DB_PASSWORD").unwrap();
    let db_name = env::var("DB_NAME").unwrap();

    let num_workers = num_cpus::get();

    let mut pg_config = tokio_postgres::Config::new();
    pg_config.user(&db_user);
    pg_config.dbname(&db_name);
    pg_config.password(db_password);
    pg_config.host(&db_host);
    let mgr_config = ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    };
    let mgr = Manager::from_config(pg_config, NoTls, mgr_config);
    let pool = Pool::builder(mgr).max_size(16).build().unwrap();

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .service(get_seoul_bus_stops)
            .service(get_ggd_bus_stops)
    })
    .workers(num_workers)
    .bind(("127.0.0.1", 11334))?
    .run()
    .await
}
