import streamlit as st
import pandas as pd
import numpy as np
import os

st.set_page_config(page_title="summary 산출 시스템", layout="wide")

st.title("summary 하는중~~~~")


# final_cost_master 에서 가져올 컬럼 (각각 합계)
COST_SUM_COLUMNS = [
    "상품매입액_합계",
    "취득세_합계",
    "매입수수료_합계",
    "폐자원공제_합계",
    "초과운행_합계",
    "차액배부_합계",
    "재료비_누적합계",
    "노무비_누적합계",
    "제조경비_누적합계",
    "매출원가_누적합계",
    "매입원가_누적합계",
    "제조원가_누적합계",
]


def _find_column(df, candidates):
    """후보 컬럼명 중 df 에 존재하는 첫 번째 반환."""
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _build_sales_division_id(sales_type_series, product_id_series):
    """'매출구분_상품ID' 형식 구분자 생성."""
    st_clean = sales_type_series.astype(str).str.strip()
    id_clean = product_id_series.astype(str).str.strip()
    return st_clean + "_" + id_clean


def load_final_cost_master():
    """final_cost_master.xlsx 불러오기 (코드 같은 위치 또는 상위)."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "final_cost_master.xlsx"),
        os.path.join(here, "..", "final_cost_master.xlsx"),
        "final_cost_master.xlsx",
    ]
    path = next((p for p in candidates if os.path.exists(p) and os.path.getsize(p) > 0), None)
    if path is None:
        return None
    try:
        return pd.read_excel(path, sheet_name="최종원가마스터")
    except Exception:
        try:
            return pd.read_excel(path)
        except Exception:
            return None


def merge_cost_into_master(master_df, final_df):
    """master_df 에 final_cost_master 의 원가 합계 컬럼을 붙인다.

    매칭: (구분자, 판매년도, 판매월) == (구분자, 회계연도, 회계월)
    각 매칭 그룹의 COST_SUM_COLUMNS 합계를 master_df 각 행에 부여.
    """
    result = master_df.copy()

    fc_sales = _find_column(final_df, ["매출구분"])
    fc_id = _find_column(final_df, ["상품ID", "상품아이디"])
    fc_year = _find_column(final_df, ["회계연도", "회계년도"])
    fc_month = _find_column(final_df, ["회계월"])

    if fc_sales is None or fc_id is None or fc_year is None or fc_month is None:
        for col in COST_SUM_COLUMNS:
            result[col] = 0
        return result

    fc = final_df.copy()
    fc["_구분자"] = _build_sales_division_id(fc[fc_sales], fc[fc_id])
    fc["_연"] = pd.to_numeric(fc[fc_year], errors="coerce")
    fc["_월"] = pd.to_numeric(fc[fc_month], errors="coerce")

    available_cols = [c for c in COST_SUM_COLUMNS if c in fc.columns]
    for c in available_cols:
        fc[c] = pd.to_numeric(fc[c], errors="coerce").fillna(0)

    grouped = (
        fc.groupby(["_구분자", "_연", "_월"], dropna=False)[available_cols].sum()
        if available_cols else None
    )

    md_year = _find_column(result, ["판매년도", "판매연도", "매출년도", "매출연도"])
    md_month = _find_column(result, ["판매월", "매출월", "월"])

    if "구분자" in result.columns:
        master_key = result["구분자"].astype(str).str.strip()
    else:
        md_id = _find_column(result, ["상품ID", "상품아이디"])
        if "상품/위탁" in result.columns and md_id is not None:
            sales_label = np.where(
                result["상품/위탁"].astype(str).str.strip() == "상품",
                "사내매출", "위탁매출",
            )
            master_key = pd.Series(sales_label, index=result.index) + "_" + result[md_id].astype(str).str.strip()
        else:
            master_key = pd.Series([""] * len(result), index=result.index)

    master_year = pd.to_numeric(result[md_year], errors="coerce") if md_year else pd.Series([pd.NA] * len(result), index=result.index)
    master_month = pd.to_numeric(result[md_month], errors="coerce") if md_month else pd.Series([pd.NA] * len(result), index=result.index)

    for col in COST_SUM_COLUMNS:
        values = []
        for key, yr, mo in zip(master_key, master_year, master_month):
            if grouped is None or col not in available_cols:
                values.append(0)
                continue
            yr_int = int(yr) if pd.notna(yr) else None
            mo_int = int(mo) if pd.notna(mo) else None
            try:
                v = grouped.loc[(key, yr_int, mo_int), col]
                values.append(float(v))
            except (KeyError, TypeError):
                values.append(0)
        result[col] = values

    return result


tab1, = st.tabs(["VIEW"])

with tab1:
    st.subheader("Summary 산출 데이터 테스트")

    master_file = "master_pnl.xlsx"
    exclude_cols = ['번호', '매입유형1', '매입유형2', '매입유형3', '매입처', '매입지점', '매입사원', '도/소매구분']

    if os.path.exists(master_file) and os.path.getsize(master_file) > 0:
        master_df = pd.read_excel(master_file)
        master_df = master_df.drop(columns=exclude_cols, errors='ignore')
        master_df['구분자'] = np.where(
            master_df['상품/위탁'] == '상품',
            '사내매출_' + master_df['상품ID'].astype(str),
            '위탁매출_' + master_df['상품ID'].astype(str),
        )

        final_df = load_final_cost_master()
        if final_df is None:
            st.warning("final_cost_master.xlsx 가 없습니다. 먼저 손익분석 페이지에서 최종 마스터를 저장하세요.")
            st.dataframe(master_df)
        else:
            merged_df = merge_cost_into_master(master_df, final_df)

            # 매출총이익 = 매출합계(master_pnl) - 매출원가_누적합계(final_cost_master)
            if "매출합계" in merged_df.columns and "매출원가_누적합계" in merged_df.columns:
                merged_df["매출총이익"] = (
                    pd.to_numeric(merged_df["매출합계"], errors="coerce").fillna(0)
                    - pd.to_numeric(merged_df["매출원가_누적합계"], errors="coerce").fillna(0)
                )

            # 판매연도 / 판매월 기간 선택 필터 (시작 ~ 끝)
            year_col = _find_column(merged_df, ["판매년도", "판매연도", "매출년도", "매출연도"])
            month_col = _find_column(merged_df, ["판매월", "매출월", "월"])

            filtered_df = merged_df
            if year_col is not None and month_col is not None:
                ym = merged_df[[year_col, month_col]].copy()
                ym[year_col] = pd.to_numeric(ym[year_col], errors="coerce")
                ym[month_col] = pd.to_numeric(ym[month_col], errors="coerce")
                ym = ym.dropna().astype(int).drop_duplicates()
                periods = sorted(
                    set(zip(ym[year_col], ym[month_col])),
                    key=lambda t: t[0] * 100 + t[1],
                )
                period_labels = [f"{y}-{m:02d}" for y, m in periods]
                period_keys = [y * 100 + m for y, m in periods]

                if period_labels:
                    col1, col2 = st.columns(2)
                    with col1:
                        start_label = st.selectbox(
                            "시작기간", period_labels, index=0, key="pl_start"
                        )
                    with col2:
                        end_label = st.selectbox(
                            "종료기간", period_labels, index=len(period_labels) - 1,
                            key="pl_end",
                        )

                    start_key = period_keys[period_labels.index(start_label)]
                    end_key = period_keys[period_labels.index(end_label)]
                    if start_key > end_key:
                        start_key, end_key = end_key, start_key

                    row_key = (
                        pd.to_numeric(filtered_df[year_col], errors="coerce").fillna(0) * 100
                        + pd.to_numeric(filtered_df[month_col], errors="coerce").fillna(0)
                    )
                    filtered_df = filtered_df[
                        (row_key >= start_key) & (row_key <= end_key)
                    ]

            st.write(f"**건수**: {len(filtered_df):,}건")
            st.dataframe(filtered_df)
    else:
        st.warning("매출, 원가파일이 비어 있습니다.")