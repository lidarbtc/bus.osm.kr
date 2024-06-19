import psycopg2
import pandas as pd

def load_csv_to_postgresql(csv_file, table_name, conn):
    # CSV 파일을 DataFrame으로 읽기
    df = pd.read_csv(csv_file)
    
    # 테이블의 기존 데이터를 삭제
    with conn.cursor() as cursor:
        cursor.execute(f"TRUNCATE TABLE {table_name} RESTART IDENTITY")
        conn.commit()

        # DataFrame을 PostgreSQL 테이블로 복사
        for i, row in df.iterrows():
            placeholders = ', '.join(['%s'] * len(row))
            columns = ', '.join(row.index)
            sql = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})"
            cursor.execute(sql, tuple(row))
        conn.commit()

def main():
    # PostgreSQL 데이터베이스에 연결
    conn = psycopg2.connect(
        host="localhost",    # PostgreSQL 서버 주소
        database="bus",  # 데이터베이스 이름
        user="bus",  # 데이터베이스 사용자
        password="1234"  # 사용자 비밀번호
    )
    
    try:
        # 경기도 버스 정류장 정보 삽입
        load_csv_to_postgresql('ggd.csv', 'ggd_bus_stops', conn)
        
        # 서울 버스 정류장 정보 삽입
        load_csv_to_postgresql('seoul.csv', 'seoul_bus_stops', conn)
    finally:
        # 연결 종료
        conn.close()

if __name__ == "__main__":
    main()
