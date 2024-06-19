CREATE TABLE ggd_bus_stops (
    시군명 TEXT,
    정류소명 TEXT,
    정류소영문명 TEXT,
    정류소id INTEGER,
    정류소번호 FLOAT,
    중앙차로여부 TEXT,
    관할관청 TEXT,
    위치 TEXT,
    WGS84위도 FLOAT,
    WGS84경도 FLOAT
);

CREATE TABLE seoul_bus_stops (
    NODE_ID INTEGER,
    ARS_ID TEXT,
    정류소명 TEXT,
    X좌표 FLOAT,
    Y좌표 FLOAT,
    정류소타입 TEXT
);
