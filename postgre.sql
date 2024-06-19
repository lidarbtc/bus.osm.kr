CREATE TABLE ggd_bus_stops (
    시군명 VARCHAR(50),
    정류소명 VARCHAR(100),
    정류소영문명 VARCHAR(100),
    정류소id INTEGER,
    정류소번호 FLOAT,
    중앙차로여부 VARCHAR(50),
    관할관청 VARCHAR(100),
    위치 VARCHAR(100),
    WGS84위도 FLOAT,
    WGS84경도 FLOAT
);

CREATE TABLE seoul_bus_stops (
    NODE_ID INTEGER,
    ARS_ID VARCHAR(10),
    정류소명 VARCHAR(100),
    X좌표 FLOAT,
    Y좌표 FLOAT,
    정류소타입 VARCHAR(50)
);
