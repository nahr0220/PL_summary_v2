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

# 텍스트 컬럼 (합계가 아니라 그룹 대표값으로 가져옴)
COST_TEXT_COLUMNS = [
    "분류1",
    "분류2",
    "분류3",
    "분류4",
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
    """cost_summary_YYYYMMDD.xlsx 중 가장 최근 파일 불러오기.

    코드 같은 위치 또는 상위 폴더에서 'cost_summary_' 로 시작하는
    .xlsx 파일을 찾아 가장 최근(파일명 정렬상 마지막) 것을 읽는다.
    """
    import glob

    here = os.path.dirname(os.path.abspath(__file__))
    search_dirs = [here, os.path.join(here, ".."), "."]

    matched = []
    for d in search_dirs:
        try:
            for p in glob.glob(os.path.join(d, "cost_summary_*.xlsx")):
                if os.path.exists(p) and os.path.getsize(p) > 0:
                    matched.append(p)
        except Exception:
            pass

    if not matched:
        return None

    # 파일명(날짜) 기준 최신 우선, 동률이면 수정시각 최신
    def sort_key(path):
        name = os.path.basename(path)
        return (name, os.path.getmtime(path))

    matched = sorted(set(matched), key=sort_key, reverse=True)
    path = matched[0]

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
        for col in COST_TEXT_COLUMNS:
            result[col] = ""
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

    # 텍스트 컬럼: 그룹 대표값(첫 값)으로 가져옴
    available_text_cols = [c for c in COST_TEXT_COLUMNS if c in fc.columns]
    grouped_text = (
        fc.groupby(["_구분자", "_연", "_월"], dropna=False)[available_text_cols].first()
        if available_text_cols else None
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

    # 텍스트 컬럼 (분류): 그룹 대표값 가져오기 (없으면 "")
    for col in COST_TEXT_COLUMNS:
        values = []
        for key, yr, mo in zip(master_key, master_year, master_month):
            if grouped_text is None or col not in available_text_cols:
                values.append("")
                continue
            yr_int = int(yr) if pd.notna(yr) else None
            mo_int = int(mo) if pd.notna(mo) else None
            try:
                v = grouped_text.loc[(key, yr_int, mo_int), col]
                values.append("" if pd.isna(v) else str(v))
            except (KeyError, TypeError):
                values.append("")
        result[col] = values

    # 원장매입일: (상품ID, 회계연도, 회계월) 매칭되는 cost_summary 의 매입일자
    #   상품/위탁 == "위탁" 이면 빈값
    fc_purchase_date = _find_column(final_df, ["매입일자", "매입일", "원장매입일"])
    purchase_date_map = {}
    if fc_purchase_date is not None:
        tmp = final_df.copy()
        tmp["_id"] = tmp[fc_id].astype(str).str.strip()
        tmp["_연"] = pd.to_numeric(tmp[fc_year], errors="coerce")
        tmp["_월"] = pd.to_numeric(tmp[fc_month], errors="coerce")
        for _, row in tmp.iterrows():
            yr = int(row["_연"]) if pd.notna(row["_연"]) else None
            mo = int(row["_월"]) if pd.notna(row["_월"]) else None
            purchase_date_map[(row["_id"], yr, mo)] = row[fc_purchase_date]

    md_id2 = _find_column(result, ["상품ID", "상품아이디"])
    is_consign = (
        result["상품/위탁"].astype(str).str.strip().eq("위탁")
        if "상품/위탁" in result.columns
        else pd.Series([False] * len(result), index=result.index)
    )
    ledger_dates = []
    for i in range(len(result)):
        if bool(is_consign.iloc[i]):
            ledger_dates.append("")
            continue
        pid = str(result[md_id2].iloc[i]).strip() if md_id2 else ""
        yr = master_year.iloc[i]
        mo = master_month.iloc[i]
        yr_int = int(yr) if pd.notna(yr) else None
        mo_int = int(mo) if pd.notna(mo) else None
        val = purchase_date_map.get((pid, yr_int, mo_int), "")
        ledger_dates.append("" if (val is None or (not isinstance(val, str) and pd.isna(val))) else val)
    result["원장매입일"] = ledger_dates

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
            st.warning("cost_summary 파일이 없습니다. 먼저 손익분석 페이지에서 최종 마스터를 저장하세요.")
            st.dataframe(master_df)
        else:
            merged_df = merge_cost_into_master(master_df, final_df)

            # 매출총이익 = 매출합계(master_pnl) - 매출원가_누적합계(final_cost_master)
            if "매출합계" in merged_df.columns and "매출원가_누적합계" in merged_df.columns:
                merged_df["매출총이익"] = (
                    pd.to_numeric(merged_df["매출합계"], errors="coerce").fillna(0)
                    - pd.to_numeric(merged_df["매출원가_누적합계"], errors="coerce").fillna(0)
                )

            # 기타현물: 분류4="C" & 분류1="현물" & 분류3 이 제외목록 아니면 "기타현물", 그 외 0
            if all(c in merged_df.columns for c in ["분류1", "분류3", "분류4"]):
                _excluded_분류3 = [
                    "오플내차팔기",
                    "오플내차팔기(바로팔기)",
                    "제휴_네이버 마이카(바로팔기)",
                    "헤이딜러",
                    "지점대차",
                ]
                _c1 = merged_df["분류1"].astype(str).str.strip()
                _c3 = merged_df["분류3"].astype(str).str.strip()
                _c4 = merged_df["분류4"].astype(str).str.strip()
                _is_etc = _c4.eq("C") & _c1.eq("현물") & ~_c3.isin(_excluded_분류3)
                merged_df["기타현물"] = pd.Series(
                    ["기타현물" if v else 0 for v in _is_etc], index=merged_df.index
                )

            # 판매일자2 = 판매일자 복사
            sale_date_col = _find_column(merged_df, ["판매일자", "판매일"])
            if sale_date_col is not None:
                merged_df["판매일자2"] = merged_df[sale_date_col]

            # 재고일수 = 분류1=="위탁" 이면 빈값, 아니면 원장매입일 - 판매일자2 + 1
            if "원장매입일" in merged_df.columns and "판매일자2" in merged_df.columns:
                ledger_dt = pd.to_datetime(merged_df["원장매입일"], errors="coerce")
                sale_dt = pd.to_datetime(merged_df["판매일자2"], errors="coerce")
                diff_days = (sale_dt - ledger_dt).dt.days + 1
                c1_consign = (
                    merged_df["분류1"].astype(str).str.strip().eq("위탁")
                    if "분류1" in merged_df.columns
                    else pd.Series([False] * len(merged_df), index=merged_df.index)
                )
                재고일수_vals = []
                for i in range(len(merged_df)):
                    if bool(c1_consign.iloc[i]):
                        재고일수_vals.append("")
                    else:
                        d = diff_days.iloc[i]
                        재고일수_vals.append("" if pd.isna(d) else int(d))
                merged_df["재고일수"] = pd.Series(재고일수_vals, index=merged_df.index)

                # 재고일수_수정 = 재고일수 <= 0 이면 1, 아니면 그대로 (빈값은 빈값)
                수정_vals = []
                for v in merged_df["재고일수"]:
                    if v == "" or pd.isna(v):
                        수정_vals.append("")
                    elif v <= 0:
                        수정_vals.append(1)
                    else:
                        수정_vals.append(int(v))
                merged_df["재고일수_수정"] = pd.Series(수정_vals, index=merged_df.index)

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