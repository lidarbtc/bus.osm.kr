CREATE TABLE seoul_bus_stops (
    정류장_ID INTEGER PRIMARY KEY,
    정류장_명칭 TEXT,
    정류장_유형 TEXT,
    정류장_번호 TEXT,
    위도 FLOAT,
    경도 FLOAT,
    버스도착정보안내기_설치_여부 TEXT
);

CREATE TABLE ggd_bus_stops (
    STATION_ID INTEGER PRIMARY KEY,
    STATION_NM TEXT,
    CENTER_ID TEXT,
    CENTER_YN TEXT,
    X FLOAT,
    Y FLOAT,
    REGION_NAME TEXT,
    MOBILE_NO TEXT,
    DISTRICT_CD TEXT
);