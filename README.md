```sh
CREATE ROLE bus_role;
GRANT USAGE ON SCHEMA public TO bus_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO bus_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bus_role;
GRANT bus_role TO bus;
```

```sh
# 'busapi' 그룹을 먼저 생성합니다.
sudo groupadd --system busapi

# 'busapi' 그룹에 속하고, 로그인 셸과 홈 디렉터리가 없는 'busapi' 시스템 유저를 생성합니다.
sudo useradd --system --gid busapi --no-create-home --shell /sbin/nologin busapi
```

```sh
sudo mv /home/ubuntu/bus-api-osm/bus-api-osm /usr/local/lib/bus-api-osm/
sudo chown -R busapi:busapi /usr/local/lib/bus-api-osm
```
