"""판매(손익) 마스터 저장소 -- SQLite 기반.

예전에는 master_pnl.xlsx/parquet 파일을 매번 통째로 읽고 합치고 다시 통째로 써서
누적했다 (analyzer.py의 save_to_master). 이 방식은 원가 마스터와 똑같이 "읽기 → 합치기
→ 쓰기" 구간이 원자적이지 않아 여러 사용자가 거의 동시에 저장하면 lost-update가 날 수
있었다. SQLite로 옮기면 트랜잭션(BEGIN IMMEDIATE)이 이 구간 전체를 원자적으로 만들어줘서
같은 문제가 근본적으로 사라진다. 서버에 이미 저장된 예전 parquet/xlsx 파일은 서버에 직접
접근할 수 없으므로, 앱이 최초 실행될 때 자동으로 SQLite로 가져온다
(_migrate_legacy_master_if_needed) -- cost_summary_builder.py의 저장소 설계를 그대로 따름.
"""

import functools
import os
import sqlite3
from datetime import date, datetime

import numpy as np
import pandas as pd

MASTER_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sales_master.db"
)
MASTER_TABLE_NAME = "sales_master"
MASTER_META_TABLE_NAME = "sales_master_meta"

# 누적 키: 새 데이터에 포함된 판매월은 기존 행을 통째로 교체 (analyzer.save_to_master 기존 로직과 동일)
_ACCUMULATION_MONTH_COLUMN = "판매월"


def _legacy_master_file_path():
    """(마이그레이션 전용) 예전 저장 방식인 master_pnl.parquet/xlsx 경로 반환 (없으면 None).

    parquet 가 있으면 우선 사용(기존 코드와 동일한 우선순위)."""
    here = os.path.dirname(os.path.abspath(__file__))
    search_dirs = [here, "."]
    for d in search_dirs:
        for name in ("master_pnl.parquet", "master_pnl.xlsx"):
            p = os.path.join(d, name)
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return p
    return None


def _read_legacy_master_file(path):
    """(마이그레이션 전용) 예전 저장 방식 파일 하나를 읽어 DataFrame 반환."""
    if path is None:
        return None
    if str(path).lower().endswith(".parquet"):
        try:
            return pd.read_parquet(path)
        except Exception:
            return None
    for engine in ("calamine", "openpyxl"):
        try:
            return pd.read_excel(path, engine=engine)
        except Exception:
            continue
    return None


def _to_sql_native(value):
    """DataFrame 셀 값을 sqlite3 모듈이 바로 바인딩할 수 있는 파이썬 기본 타입으로 변환."""
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        # 먼저 결측(NaT)인지 확인 -- 안 그러면 str(pd.NaT) == "NaT" 라는 문자열이 그대로
        # 저장되어 화면에 "NaT" 글자가 찍혀버린다.
        if pd.isna(value):
            return None
        # .isoformat()이 아니라 str()을 쓴다 -- isoformat()은 "2026-07-10T00:00:00"처럼
        # T로 구분하는데, 화면 표시 로직(dataframe_for_display/_strip_zero_time)은
        # 예전 parquet 시절부터 "2026-07-10 00:00:00"(공백 구분) 형식만 인식해서 시간을
        # 지워왔다. str()은 그 공백 구분 형식을 그대로 만들어줘서 기존 로직과 호환된다.
        return str(value)
    if isinstance(value, np.floating):
        value = float(value)
    elif isinstance(value, np.integer):
        return int(value)
    elif isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, float) and pd.isna(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _write_master_table(conn, df):
    """DataFrame 을 마스터 테이블에 통째로 다시 씀 (DROP + CREATE + INSERT).

    pandas.DataFrame.to_sql 은 내부적으로 자체 commit 을 호출해버려서, 수동으로 연
    BEGIN IMMEDIATE 트랜잭션을 중간에 끊어버린다 -- executemany 로 직접 써서 트랜잭션이
    끝까지 우리 손 안에 있게 한다."""
    columns = list(df.columns)
    conn.execute(f"DROP TABLE IF EXISTS {MASTER_TABLE_NAME}")
    col_defs = ", ".join(f'"{c}"' for c in columns)
    conn.execute(f"CREATE TABLE {MASTER_TABLE_NAME} ({col_defs})")
    if not columns:
        return
    placeholders = ", ".join(["?"] * len(columns))
    rows = [
        tuple(_to_sql_native(v) for v in row)
        for row in df.itertuples(index=False, name=None)
    ]
    if rows:
        conn.executemany(
            f"INSERT INTO {MASTER_TABLE_NAME} ({col_defs}) VALUES ({placeholders})", rows,
        )


def _master_table_exists(conn):
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (MASTER_TABLE_NAME,),
    )
    return cur.fetchone() is not None


def _migrate_legacy_master_if_needed(conn):
    """예전 master_pnl.parquet/xlsx 가 있으면 SQLite로 딱 한 번만 자동 가져온다.

    meta 테이블에 'legacy_migrated' 플래그를 남겨, 이후 마스터를 초기화(delete)해도
    예전 파일이 남아있다고 해서 다시 되살아나지 않게 한다."""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {MASTER_META_TABLE_NAME} (key TEXT PRIMARY KEY, value TEXT)"
    )
    cur = conn.execute(
        f"SELECT value FROM {MASTER_META_TABLE_NAME} WHERE key='legacy_migrated'"
    )
    if cur.fetchone() is not None:
        return

    legacy_path = _legacy_master_file_path()
    if legacy_path is not None:
        legacy_df = _read_legacy_master_file(legacy_path)
        if legacy_df is not None and not legacy_df.empty:
            _write_master_table(conn, legacy_df)
            saved_at = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                f"INSERT OR REPLACE INTO {MASTER_META_TABLE_NAME} (key, value) VALUES ('saved_at', ?)",
                (saved_at,),
            )

    conn.execute(
        f"INSERT OR REPLACE INTO {MASTER_META_TABLE_NAME} (key, value) VALUES ('legacy_migrated', '1')"
    )
    conn.commit()


def _get_master_db_connection():
    """마스터 SQLite DB 연결 반환 (필요 시 예전 파일에서 자동 마이그레이션 수행)."""
    conn = sqlite3.connect(MASTER_DB_PATH, timeout=30)
    conn.isolation_level = None  # 수동으로 BEGIN IMMEDIATE 트랜잭션을 제어하기 위해 autocommit 모드 사용
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate_legacy_master_if_needed(conn)
    return conn


def master_state_fingerprint():
    """마스터가 바뀌면 값이 달라지는 지문 (캐시 무효화용). 마스터가 없으면 None."""
    try:
        conn = _get_master_db_connection()
    except Exception:
        return None
    try:
        if not _master_table_exists(conn):
            return None
        cur = conn.execute(
            f"SELECT value FROM {MASTER_META_TABLE_NAME} WHERE key='saved_at'"
        )
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        conn.close()


def _reorder_sales_master_columns(df):
    """'>>컬럼구분>>'을 '판매연도' 바로 왼쪽으로, 'updated_at'을 맨 마지막으로 정렬.

    (analyzer.save_to_master 의 기존 컬럼 정렬 로직을 그대로 이관)"""
    if ">>컬럼구분>>" in df.columns and "판매연도" in df.columns:
        cols = [c for c in df.columns if c != ">>컬럼구분>>"]
        idx = cols.index("판매연도")
        cols = cols[:idx] + [">>컬럼구분>>"] + cols[idx:]
        df = df[cols]
    if "updated_at" in df.columns:
        cols = [c for c in df.columns if c != "updated_at"] + ["updated_at"]
        df = df[cols]
    return df


def _accumulate_sales_master_data(existing_df, new_df):
    """기존 마스터에 새 데이터를 누적.

    키 = 판매월 단위: 새 데이터에 포함된 판매월은 기존 행을 통째로 교체, 없는 판매월은 그대로 유지.
    (상품ID 등 세부 키가 아니라 월 전체를 교체하는 기존 save_to_master 로직과 동일)
    반환: 누적 df.
    """
    new_df = new_df.copy()
    if _ACCUMULATION_MONTH_COLUMN in new_df.columns:
        new_df[_ACCUMULATION_MONTH_COLUMN] = pd.to_numeric(
            new_df[_ACCUMULATION_MONTH_COLUMN], errors="coerce"
        )

    if (
        existing_df is None
        or existing_df.empty
        or _ACCUMULATION_MONTH_COLUMN not in existing_df.columns
        or _ACCUMULATION_MONTH_COLUMN not in new_df.columns
    ):
        return _reorder_sales_master_columns(new_df)

    existing_df = existing_df.copy()
    existing_df[_ACCUMULATION_MONTH_COLUMN] = pd.to_numeric(
        existing_df[_ACCUMULATION_MONTH_COLUMN], errors="coerce"
    )
    update_months = new_df[_ACCUMULATION_MONTH_COLUMN].dropna().unique()
    existing_df = existing_df[~existing_df[_ACCUMULATION_MONTH_COLUMN].isin(update_months)]

    combined = pd.concat([existing_df, new_df], ignore_index=True, sort=False)
    return _reorder_sales_master_columns(combined)


def save_sales_master(new_df):
    """새 판매 데이터를 누적해 SQLite 마스터에 저장.

    BEGIN IMMEDIATE 트랜잭션으로 '읽기 → 합치기 → 쓰기' 구간 전체를 원자적으로 만들어,
    여러 사용자가 거의 동시에 저장해도 서로 덮어쓰지 않는다.

    반환: (저장 시각, 누적 후 총 행 수)
    """
    saved_at = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(MASTER_DB_PATH, timeout=30)
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _migrate_legacy_master_if_needed(conn)

        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = (
                pd.read_sql(f"SELECT * FROM {MASTER_TABLE_NAME}", conn)
                if _master_table_exists(conn) else None
            )
            combined = _accumulate_sales_master_data(existing, new_df)

            _write_master_table(conn, combined)
            conn.execute(
                f"INSERT OR REPLACE INTO {MASTER_META_TABLE_NAME} (key, value) VALUES ('saved_at', ?)",
                (saved_at,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return saved_at, len(combined)


def read_sales_master():
    """저장된 판매 마스터 불러오기. (df, 저장시각) 또는 (None, None)."""
    conn = _get_master_db_connection()
    try:
        if not _master_table_exists(conn):
            return None, None
        df = pd.read_sql(f"SELECT * FROM {MASTER_TABLE_NAME}", conn)
        saved_at = None
        try:
            cur = conn.execute(
                f"SELECT value FROM {MASTER_META_TABLE_NAME} WHERE key='saved_at'"
            )
            row = cur.fetchone()
            saved_at = row[0] if row else None
        except Exception:
            pass
        return df, saved_at
    except Exception:
        return None, None
    finally:
        conn.close()


def delete_sales_master():
    """마스터 데이터 전체 삭제. 삭제 성공 여부 반환.

    legacy_migrated 플래그는 지우지 않는다 -- 지우면 예전 parquet/xlsx 파일이 남아있는 경우
    다음 접근 때 다시 자동 마이그레이션되어 방금 지운 데이터가 되살아나 버린다."""
    conn = _get_master_db_connection()
    try:
        deleted = False
        if _master_table_exists(conn):
            conn.execute(f"DROP TABLE {MASTER_TABLE_NAME}")
            deleted = True
        conn.execute(
            f"DELETE FROM {MASTER_META_TABLE_NAME} WHERE key='saved_at'"
        )
        return deleted
    finally:
        conn.close()


@functools.lru_cache(maxsize=2)
def _get_product_id_period_counts_cached(fingerprint):
    conn = _get_master_db_connection()
    try:
        if not _master_table_exists(conn):
            return {}
        # 실제 컬럼명은 앞뒤 공백이 섞여 있을 수 있어(엑셀에서 흔함), 매칭은 strip 한 이름으로 하되
        # SQL 에는 원래 컬럼명 그대로 참조해야 함 (_strip_columns 적용 후 매칭하던 기존 로직과 동일 의미)
        cols_raw = [row[1] for row in conn.execute(f'PRAGMA table_info("{MASTER_TABLE_NAME}")')]
        cols_stripped = {c.strip(): c for c in cols_raw}

        def _find_column(candidates):
            for c in candidates:
                if c in cols_stripped:
                    return cols_stripped[c]
            return None

        id_column = _find_column(["상품ID", "상품아이디", "차량아이디", "CODE"])
        if id_column is None:
            return {}
        year_column = _find_column(["판매연도", "판매년도", "매출연도", "매출년도", "연도", "년도"])
        month_column = _find_column(["판매월", "판매달", "매출월", "월"])

        select_cols = [f'"{id_column}" AS "상품ID"']
        select_cols.append(f'"{year_column}" AS "연"' if year_column else 'NULL AS "연"')
        select_cols.append(f'"{month_column}" AS "월"' if month_column else 'NULL AS "월"')
        df = pd.read_sql(
            f'SELECT {", ".join(select_cols)} FROM {MASTER_TABLE_NAME}', conn,
        )
    finally:
        conn.close()

    df["상품ID"] = df["상품ID"].astype(str).str.strip()
    df["연"] = pd.to_numeric(df["연"], errors="coerce")
    df["월"] = pd.to_numeric(df["월"], errors="coerce")
    df = df[df["상품ID"].ne("")]
    if df.empty:
        return {}

    result = {}
    grouped = df.groupby(["상품ID", "연", "월"], dropna=False).size()
    for (pid, year, month), count in grouped.items():
        year_int = int(year) if pd.notna(year) else None
        month_int = int(month) if pd.notna(month) else None
        result[(pid, year_int, month_int)] = int(count)
    return result


def get_product_id_period_counts():
    """판매 마스터에서 (상품ID, 판매연도, 판매월) 조합별 개수 반환.

    반환: {(상품ID, 연, 월): 개수}. 마스터가 없거나 상품ID 컬럼이 없으면 {}.
    마스터가 바뀌지 않았으면 캐시 재사용."""
    return _get_product_id_period_counts_cached(master_state_fingerprint())
