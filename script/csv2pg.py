import pandas as pd
import psycopg2


def load_csv_to_postgresql(
    csv_file, table_name, conn, delimiter=",", line_terminator="\n"
):
    # CSV 파일을 DataFrame으로 읽기
    if table_name == "seoul_bus_stops":
        seoul_dtypes = {"ARS_ID": str}
        df = pd.read_csv(csv_file, delimiter=delimiter, dtype=seoul_dtypes)
    else:
        ggd_dtypes = {"mobileNo": str}
        df = pd.read_csv(
            csv_file,
            delimiter=delimiter,
            lineterminator=line_terminator,
            dtype=ggd_dtypes,
        )

    # ggd_bus_stops 테이블에 대한 특별 처리
    if table_name == "ggd_bus_stops":
        column_mapping = {
            "stationId": "station_id",
            "stationName": "station_nm",
            "centerYn": "center_yn",
            "regionName": "region_name",
            "mobileNo": "mobile_no",
        }
        df.rename(columns=column_mapping, inplace=True)
        df["center_id"] = None
        df["district_cd"] = None
        db_columns_order = [
            "station_id",
            "station_nm",
            "center_id",
            "center_yn",
            "x",
            "y",
            "region_name",
            "mobile_no",
            "district_cd",
        ]
        df = df[db_columns_order]

    # seoul_bus_stops 테이블에 대한 특별 처리
    elif table_name == "seoul_bus_stops":
        column_mapping = {
            "NODE_ID": "정류장_ID",
            "ARS_ID": "정류장_번호",
            "정류소명": "정류장_명칭",
            "X좌표": "경도",
            "Y좌표": "위도",
            "정류소타입": "정류장_유형",
        }
        df.rename(columns=column_mapping, inplace=True)
        df["버스도착정보안내기_설치_여부"] = None
        db_columns_order = [
            "정류장_ID",
            "정류장_명칭",
            "정류장_유형",
            "정류장_번호",
            "위도",
            "경도",
            "버스도착정보안내기_설치_여부",
        ]
        df = df[db_columns_order]

    # 테이블의 기존 데이터를 삭제
    with conn.cursor() as cursor:
        cursor.execute(f"DELETE FROM {table_name}")
        conn.commit()

        # DataFrame을 PostgreSQL 테이블로 복사
        for i, row in df.iterrows():
            placeholders = ", ".join(["%s"] * len(row))
            columns = ", ".join([col.lower() for col in row.index])
            sql = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})"

            # .where(pd.notna(row), None)은 NaN, NaT 등을 None으로 바꿔줌
            # 문자열로 읽었기 때문에 빈 문자열이 있을 수 있으므로, 이를 None으로 처리하는 로직 추가
            data_tuple = tuple(row.replace("", None).where(pd.notna(row), None))

            cursor.execute(sql, data_tuple)
        conn.commit()


def main():
    # PostgreSQL 데이터베이스에 연결
    conn = psycopg2.connect(
        host="localhost",
        database="bus",
        user="bus",
        password="1234",
    )

    try:
        # 경기도 버스 정류장 정보 삽입
        load_csv_to_postgresql(
            "ggd.csv", "ggd_bus_stops", conn, delimiter="|", line_terminator="^"
        )

        # 서울 버스 정류장 정보 삽입
        load_csv_to_postgresql("seoul.csv", "seoul_bus_stops", conn)
    finally:
        # 연결 종료
        conn.close()


if __name__ == "__main__":
    main()
