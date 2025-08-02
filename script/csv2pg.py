import pandas as pd
import psycopg2


def load_csv_to_postgresql(
    csv_file, table_name, conn, delimiter=",", line_terminator="\n"
):
    if table_name == "seoul_bus_stops":
        df = pd.read_csv(
            csv_file,
            delimiter=delimiter,
            dtype={"ARS_ID": str},
            keep_default_na=False,
            na_values=[""],
        )
    else:
        df = pd.read_csv(
            csv_file,
            delimiter=delimiter,
            lineterminator=line_terminator,
            dtype={"mobileNo": str},
            keep_default_na=False,
            na_values=[""],
        )

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

    with conn.cursor() as cursor:
        cursor.execute(f"DELETE FROM {table_name}")
        conn.commit()

        for i, row in df.iterrows():
            placeholders = ", ".join(["%s"] * len(row))
            columns = ", ".join([col.lower() for col in row.index])
            sql = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})"
            data_tuple = tuple(row.where(pd.notna(row), None))
            cursor.execute(sql, data_tuple)
        conn.commit()


def main():
    conn = psycopg2.connect(
        host="localhost",
        database="bus",
        user="bus",
        password="1234",
    )

    try:
        load_csv_to_postgresql(
            "ggd.csv", "ggd_bus_stops", conn, delimiter="|", line_terminator="^"
        )
        load_csv_to_postgresql("seoul.csv", "seoul_bus_stops", conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
