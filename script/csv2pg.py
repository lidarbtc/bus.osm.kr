import pandas as pd
import psycopg2


def load_csv_to_postgresql(
    csv_file, table_name, conn, delimiter=",", line_terminator="\n"
):
    # CSV 파일을 DataFrame으로 읽기
    df = pd.read_csv(csv_file, delimiter=delimiter, lineterminator=line_terminator)

    # ggd_bus_stops 테이블에 대한 특별 처리
    if table_name == "ggd_bus_stops":
        # 1. CSV의 컬럼명을 DB 스키마에 맞게 변경 (camelCase -> snake_case)
        column_mapping = {
            "stationId": "station_id",
            "stationName": "station_nm",
            "centerYn": "center_yn",
            "regionName": "region_name",
            "mobileNo": "mobile_no",
            # 'x'와 'y'는 이름이 같으므로 매핑 불필요
        }
        df.rename(columns=column_mapping, inplace=True)

        # 2. CSV에 없는 컬럼을 추가하고 None(NULL)으로 채우기
        df["center_id"] = None
        df["district_cd"] = None

        # 3. DB 테이블의 컬럼 순서에 맞게 DataFrame의 컬럼 순서를 재정렬
        #    이렇게 하면 INSERT 문이 안정적으로 동작합니다.
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

    # 테이블의 기존 데이터를 삭제
    with conn.cursor() as cursor:
        cursor.execute(f"DELETE FROM {table_name}")
        conn.commit()

        # DataFrame을 PostgreSQL 테이블로 복사
        for i, row in df.iterrows():
            # 컬럼 순서를 위에서 맞췄기 때문에 row.index를 그대로 사용해도 안전합니다.
            placeholders = ", ".join(["%s"] * len(row))
            columns = ", ".join(row.index)
            sql = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})"

            # NaN 값을 None으로 변환하여 SQL의 NULL로 입력되도록 처리
            # pandas에서 빈 값은 종종 NaN(Not a Number)으로 읽히기 때문입니다.
            data_tuple = tuple(row.where(pd.notna(row), None))

            cursor.execute(sql, data_tuple)
        conn.commit()


def main():
    # PostgreSQL 데이터베이스에 연결
    conn = psycopg2.connect(
        host="localhost",  # PostgreSQL 서버 주소
        database="bus",  # 데이터베이스 이름
        user="bus",  # 데이터베이스 사용자
        password="1234",  # 사용자 비밀번호
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
