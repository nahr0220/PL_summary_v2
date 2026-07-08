import streamlit as st
import pandas as pd
import numpy as np
import os

st.set_page_config(page_title="summary 산출 시스템", layout="wide")

st.title("손익 분석 Summary")


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


@st.cache_data(show_spinner=False, max_entries=2)
def _read_excel_cached(path, mtime, sheet_name=None):
    """파일 수정시간을 캐시 키에 포함해 같은 엑셀 반복 읽기를 줄인다.

    max_entries 를 작게 유지해 큰 DataFrame 버전이 서버 메모리에 계속 쌓이지 않게 함.

    parquet 면 그대로 읽고(sheet_name 무시), xlsx 면 calamine → openpyxl 순으로 폴백."""
    if str(path).lower().endswith(".parquet"):
        return pd.read_parquet(path)
    for engine in ("calamine", "openpyxl"):
        try:
            if sheet_name is None:
                return pd.read_excel(path, engine=engine)
            return pd.read_excel(path, sheet_name=sheet_name, engine=engine)
        except Exception:
            continue
    raise RuntimeError(f"엑셀 파일을 읽을 수 없습니다: {path}")


def _memoize(cache_name, fingerprint, compute_fn):
    """fingerprint 가 이전 호출과 같으면 session_state 에 저장된 결과를 재사용.

    위젯 조작으로 스크립트가 재실행돼도 관련 없는 변경이면 무거운 재계산(엑셀 생성 등)을 건너뜀."""
    cache_key = f"_memo_{cache_name}"
    cached = st.session_state.get(cache_key)
    if cached is not None and cached.get("fingerprint") == fingerprint:
        return cached["result"]
    result = compute_fn()
    st.session_state[cache_key] = {"fingerprint": fingerprint, "result": result}
    return result


def load_final_cost_master():
    """cost_summary_YYYYMMDD.xlsx 중 가장 최근 파일 불러오기.

    코드 같은 위치 또는 상위 폴더에서 'cost_summary_' 로 시작하는
    .xlsx 파일을 찾아 가장 최근(파일명 정렬상 마지막) 것을 읽는다.
    """
    import glob

    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)  # 부모 폴더 (코드가 pages 안에 있으면 앱 루트)
    search_dirs = [parent, here, "."]

    matched = []
    for d in search_dirs:
        try:
            candidates = (
                glob.glob(os.path.join(d, "cost_summary_*.parquet"))
                + glob.glob(os.path.join(d, "cost_summary_*.xlsx"))
            )
            for p in candidates:
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

    mtime = os.path.getmtime(path)
    try:
        return _read_excel_cached(path, mtime, "최종원가마스터")
    except Exception:
        try:
            return _read_excel_cached(path, mtime, None)
        except Exception:
            return None


def merge_cost_into_master(master_df, final_df):
    """master_df 에 final_cost_master 의 원가 합계 컬럼을 붙인다.

    매칭: (구분자, 판매년도, 판매월) == (구분자, 회계연도, 회계월)
    각 매칭 그룹의 COST_SUM_COLUMNS 합계를 master_df 각 행에 부여.
    """
    result = master_df.copy()

    def _to_int_key(series, index=None):
        if series is None:
            return pd.Series(pd.array([pd.NA] * len(index), dtype="Int64"), index=index)
        numeric = pd.to_numeric(series, errors="coerce")
        truncated = numeric.copy()
        mask = numeric.notna()
        truncated.loc[mask] = np.trunc(numeric.loc[mask].astype(float))
        return truncated.astype("Int64")

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
    fc["_연"] = _to_int_key(fc[fc_year])
    fc["_월"] = _to_int_key(fc[fc_month])
    fc_valid = fc[fc["_연"].notna() & fc["_월"].notna()].copy()

    available_cols = [c for c in COST_SUM_COLUMNS if c in fc.columns]
    for c in available_cols:
        fc_valid[c] = pd.to_numeric(fc_valid[c], errors="coerce").fillna(0)

    # 텍스트 컬럼: 그룹 대표값(첫 값)으로 가져옴
    available_text_cols = [c for c in COST_TEXT_COLUMNS if c in fc.columns]

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

    master_year = _to_int_key(result[md_year], result.index) if md_year else _to_int_key(None, result.index)
    master_month = _to_int_key(result[md_month], result.index) if md_month else _to_int_key(None, result.index)

    key_columns = ["_구분자", "_연", "_월"]
    lookup_keys = pd.DataFrame({
        "_row_pos": np.arange(len(result)),
        "_구분자": master_key.to_numpy(),
        "_연": master_year.to_numpy(),
        "_월": master_month.to_numpy(),
    })

    if available_cols and not fc_valid.empty:
        grouped = (
            fc_valid.groupby(key_columns, dropna=False)[available_cols]
            .sum()
            .reset_index()
        )
        cost_values = lookup_keys.merge(grouped, on=key_columns, how="left", sort=False)
    else:
        cost_values = lookup_keys

    for col in COST_SUM_COLUMNS:
        if col in available_cols and col in cost_values.columns:
            result[col] = (
                pd.to_numeric(cost_values[col], errors="coerce")
                .fillna(0)
                .to_numpy()
            )
        else:
            result[col] = 0

    # 텍스트 컬럼 (분류): 그룹 대표값 가져오기 (없으면 "")
    if available_text_cols and not fc_valid.empty:
        grouped_text = (
            fc_valid.groupby(key_columns, dropna=False)[available_text_cols]
            .first()
            .reset_index()
        )
        text_values = lookup_keys.merge(grouped_text, on=key_columns, how="left", sort=False)
    else:
        text_values = lookup_keys

    for col in COST_TEXT_COLUMNS:
        if col in available_text_cols and col in text_values.columns:
            result[col] = text_values[col].where(text_values[col].notna(), "").astype(str).to_numpy()
        else:
            result[col] = ""

    # 원장매입일: (상품ID, 회계연도, 회계월) 매칭되는 cost_summary 의 매입일자
    #   상품/위탁 == "위탁" 이면 빈값
    fc_purchase_date = _find_column(final_df, ["매입일자", "매입일", "원장매입일"])
    md_id2 = _find_column(result, ["상품ID", "상품아이디"])
    is_consign = (
        result["상품/위탁"].astype(str).str.strip().eq("위탁")
        if "상품/위탁" in result.columns
        else pd.Series([False] * len(result), index=result.index)
    )
    if fc_purchase_date is not None and md_id2 is not None:
        tmp = final_df[[fc_id, fc_year, fc_month, fc_purchase_date]].copy()
        tmp["_id"] = tmp[fc_id].astype(str).str.strip()
        tmp["_연"] = _to_int_key(tmp[fc_year])
        tmp["_월"] = _to_int_key(tmp[fc_month])
        tmp = tmp[tmp["_연"].notna() & tmp["_월"].notna()].copy()
        tmp = tmp.drop_duplicates(subset=["_id", "_연", "_월"], keep="last")

        date_keys = pd.DataFrame({
            "_id": result[md_id2].astype(str).str.strip().to_numpy(),
            "_연": master_year.to_numpy(),
            "_월": master_month.to_numpy(),
        })
        date_values = date_keys.merge(
            tmp[["_id", "_연", "_월", fc_purchase_date]],
            on=["_id", "_연", "_월"],
            how="left",
            sort=False,
        )
        ledger_dates = date_values[fc_purchase_date].astype("object")
        ledger_dates = ledger_dates.where(ledger_dates.notna(), "")
        ledger_dates = ledger_dates.to_numpy(dtype=object)
        ledger_dates[is_consign.to_numpy()] = ""
        result["원장매입일"] = ledger_dates
    else:
        result["원장매입일"] = ""

    return result


tab1, = st.tabs(["VIEW"])

with tab1:
    master_file = "master_pnl.xlsx"
    exclude_cols = ['번호', '매입유형1', '매입유형2', '매입유형3', '매입처', '매입지점', '매입사원', '도/소매구분']

    if os.path.exists(master_file) and os.path.getsize(master_file) > 0:
        master_df = _read_excel_cached(master_file, os.path.getmtime(master_file), None)
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
                valid_days = ~c1_consign & diff_days.notna()
                merged_df["재고일수"] = ""
                merged_df.loc[valid_days, "재고일수"] = (
                    diff_days.loc[valid_days].astype(int).astype("object")
                )

                # 재고일수_수정 = 재고일수 <= 0 이면 1, 아니면 그대로 (빈값은 빈값)
                numeric_days = pd.to_numeric(merged_df["재고일수"], errors="coerce")
                valid_adjusted = numeric_days.notna()
                merged_df["재고일수_수정"] = ""
                merged_df.loc[valid_adjusted, "재고일수_수정"] = (
                    numeric_days.loc[valid_adjusted]
                    .where(numeric_days.loc[valid_adjusted].gt(0), 1)
                    .astype(int)
                    .astype("object")
                )

            # ===== 월별 판매 대수 / 월별 매출 (merged_df 기반) =====
            if '소/도매' in merged_df.columns and '판매월' in merged_df.columns:
                order = ['소매', '도매']
                merged_df['소/도매'] = pd.Categorical(
                    merged_df['소/도매'], categories=order, ordered=True
                )

                if not merged_df.empty:
                    monthly_df = merged_df
                    if '판매연도' in merged_df.columns:
                        _monthly_years = sorted(merged_df['판매연도'].dropna().unique().tolist(), reverse=True)
                        if _monthly_years:
                            _monthly_year_col, _ = st.columns([1, 3])
                            with _monthly_year_col:
                                _monthly_selected_years = st.multiselect(
                                    "판매연도", _monthly_years,
                                    default=[_monthly_years[0]], key="summary_monthly_years",
                                )
                            if not _monthly_selected_years:
                                st.info("판매연도를 선택하세요.")
                                monthly_df = merged_df.iloc[0:0]
                            else:
                                monthly_df = merged_df[merged_df['판매연도'].isin(_monthly_selected_years)].copy()

                    _toggle_l, _toggle_r = st.columns([9, 1])
                    with _toggle_r:
                        if hasattr(st, "toggle"):
                            show_per_unit_only = st.toggle(
                                "대당으로 보기",
                                value=False,
                                key="summary_show_per_unit_only",
                            )
                        else:
                            show_per_unit_only = st.checkbox(
                                "대당으로 보기",
                                value=False,
                                key="summary_show_per_unit_only",
                            )

                    def _amount_caption(label):
                        if show_per_unit_only:
                            return f"(단위: {label} 원/대)"
                        return "(단위: 원)"

                    def _summary_table_height(df, row_height=35, header_height=38):
                        """Set monthly summary table height to show all rows."""
                        return int(header_height + row_height * len(df) + 4)

                    # 월별 표들(판매대수/매출/매출원가/매출총이익/매출총이익률) 사이에
                    # 월(1~12월/연간 총합) 컬럼 경계선이 서로 일치하도록 폭을 통일한다.
                    _MONTH_TABLE_LABEL_WIDTH = 170
                    _MONTH_TABLE_DATA_WIDTH = 95
                    _MONTH_TABLE_INDENT = "    "

                    def _month_table_column_config(data_columns):
                        cfg = {"구분": st.column_config.Column(width=_MONTH_TABLE_LABEL_WIDTH)}
                        for col in data_columns:
                            cfg[col] = st.column_config.Column(width=_MONTH_TABLE_DATA_WIDTH)
                        return cfg

                    def _flatten_dense_multiindex(df, top_label='전체'):
                        """(대분류, 소도매) 2단 인덱스 → '구분' 컬럼 하나로 합친 flat df.
                        소도매가 공백(소계 행)이거나 top_label(전체) 이면 대분류만, 그 외엔 들여쓴 소도매만 표시."""
                        labels = []
                        highlight_positions = set()
                        for pos, (l0, l1) in enumerate(df.index):
                            l0s, l1s = str(l0).strip(), str(l1).strip()
                            if l0s == top_label:
                                labels.append(l0s)
                                highlight_positions.add(pos)
                            elif not l1s:
                                labels.append(l0s)
                            else:
                                labels.append(_MONTH_TABLE_INDENT + l1s)
                        flat = df.reset_index(drop=True)
                        flat.insert(0, '구분', labels)
                        return flat, highlight_positions

                    def _flatten_sparse_labels(raw_tuples):
                        """레벨 중 값이 있는 가장 깊은 레벨만, 그 깊이만큼 들여써서 한 줄 라벨로."""
                        labels = []
                        for t in raw_tuples:
                            depth, text = 0, ""
                            for i, v in enumerate(t):
                                vs = str(v).strip()
                                if vs:
                                    depth, text = i, vs
                            labels.append(_MONTH_TABLE_INDENT * depth + text)
                        return labels

                    def _render_month_table(title, unit_caption, flat_df, highlight_positions, numeric_format=None):
                        """'구분' + 월별 데이터 컬럼인 flat_df 를 모든 월별 표와 동일한 컬럼 폭으로 렌더링."""
                        data_columns = [c for c in flat_df.columns if c != '구분']
                        style_df = pd.DataFrame("", index=flat_df.index, columns=flat_df.columns)
                        for r in range(len(flat_df)):
                            row_style = (
                                "background-color: #e6f3ff; font-weight: bold; border-top: 2px solid #004c99;"
                                if r in highlight_positions else ""
                            )
                            style_df.iloc[r, :] = row_style

                        styler = flat_df.style
                        if numeric_format is not None:
                            styler = styler.format(numeric_format, subset=data_columns)
                        styler = (
                            styler
                            .set_properties(subset=data_columns, **{'text-align': 'right', 'font-size': '13px'})
                            .set_properties(subset=['구분'], **{'text-align': 'left', 'font-size': '13px', 'white-space': 'pre'})
                            .apply(lambda _: style_df, axis=None)
                        )

                        st.markdown(
                            f"""
                            <div style="display:flex; justify-content:space-between; align-items:flex-end;">
                                <div style="font-size:20px; font-weight:bold;">{title}</div>
                                <div style="font-size:12px; color:gray;">{unit_caption}</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                        st.dataframe(
                            styler,
                            use_container_width=True,
                            hide_index=True,
                            height=_summary_table_height(flat_df),
                            column_config=_month_table_column_config(data_columns),
                        )

                    # 1. 월별 판매 대수 (상품/위탁 × 소/도매)
                    s_p = monthly_df.pivot_table(
                        index=['상품/위탁', '소/도매'], columns='판매월',
                        values='상품ID', aggfunc='count', fill_value=0, observed=False,
                    ).astype(int)
                    s_p = s_p.reindex(columns=range(1, 13), fill_value=0)
                    s_p['연간 총합'] = s_p.sum(axis=1)
                    subtotals_s = s_p.groupby(level=0).sum()
                    subtotals_s.index = pd.MultiIndex.from_tuples(
                        [(x, ' ') for x in subtotals_s.index]
                    )
                    s_p = pd.concat([s_p, subtotals_s]).sort_index()
                    s_p.loc[('전체', '총 판매대수'), :] = subtotals_s.sum(axis=0).values

                    flat_s_p, hl_s_p = _flatten_dense_multiindex(s_p)
                    _render_month_table(
                        "월별 판매 대수", "(단위: 대)", flat_s_p, hl_s_p,
                        numeric_format=lambda x: '-' if x == 0 else f"{x:,.0f}",
                    )

                    # 2. 월별 매출 (상품매출 / 용역매출 × 소/도매)
                    if all(c in merged_df.columns for c in ['상품매출', '용역매출']):
                        # 매출 = 상품매출 + 용역매출 (행별 합)
                        monthly_df["_매출합"] = (
                            pd.to_numeric(monthly_df['상품매출'], errors='coerce').fillna(0)
                            + pd.to_numeric(monthly_df['용역매출'], errors='coerce').fillna(0)
                        )
                        # 판매대수 표와 동일 기준: 상품/위탁 × 소/도매
                        r_p = monthly_df.pivot_table(
                            index=['상품/위탁', '소/도매'], columns='판매월',
                            values='_매출합', aggfunc='sum', fill_value=0, observed=False,
                        ).astype(int)
                        r_p = r_p.reindex(columns=range(1, 13), fill_value=0)
                        r_p['연간 총합'] = r_p.sum(axis=1)
                        subtotals = r_p.groupby(level=0).sum()
                        subtotals.index = pd.MultiIndex.from_tuples(
                            [(x, ' ') for x in subtotals.index]
                        )
                        r_p = pd.concat([r_p, subtotals]).sort_index()
                        r_p.loc[('전체', '총 매출액'), :] = subtotals.sum(axis=0).values

                        # 대당 매출 계산 (상세 행은 그대로 나눔, 전체 행은 별도)
                        s_p_calc = s_p.drop(index='전체', errors='ignore') if '전체' in s_p.index.get_level_values(0) else s_p
                        r_p_calc = r_p.drop(index='전체', errors='ignore') if '전체' in r_p.index.get_level_values(0) else r_p

                        with np.errstate(divide='ignore', invalid='ignore'):
                            per_unit = r_p_calc.astype(float) / s_p_calc.astype(float)
                        per_unit = per_unit.replace([np.inf, -np.inf], 0).fillna(0).round(0).astype(int)

                        total_sales = r_p_calc.sum(axis=0)
                        total_qty = s_p_calc.sum(axis=0)
                        with np.errstate(divide='ignore', invalid='ignore'):
                            total_row = (total_sales.astype(float) / total_qty.astype(float))
                        total_row = total_row.replace([np.inf, -np.inf], 0).fillna(0).round(0).astype(int)
                        # per_unit 의 인덱스가 매출 표와 같으니, '전체' 행도 같은 라벨로 추가
                        per_unit.loc[('전체', '총 매출액'), :] = total_row.values

                        # 표시 방식: 기본은 금액만, 토글 ON 은 대당 금액만.
                        def _fmt_cell(amount, per):
                            amount_int = int(amount) if pd.notna(amount) else 0
                            per_int = int(per) if pd.notna(per) else 0
                            if show_per_unit_only:
                                return '-' if per_int == 0 else f"{per_int:,}"
                            return '-' if amount_int == 0 else f"{amount_int:,}"

                        combined = pd.DataFrame(index=r_p.index, columns=r_p.columns, dtype=object)
                        for idx in r_p.index:
                            for col in r_p.columns:
                                amt = r_p.loc[idx, col]
                                try:
                                    pu = per_unit.loc[idx, col]
                                except KeyError:
                                    pu = 0
                                combined.loc[idx, col] = _fmt_cell(amt, pu)

                        flat_combined, hl_combined = _flatten_dense_multiindex(combined)
                        _render_month_table(
                            "월별 매출액", _amount_caption("대당 매출"), flat_combined, hl_combined,
                        )

                        # 임시 컬럼 정리
                        monthly_df.drop(columns=['_매출합'], inplace=True)

                    # ===== 월별 매출원가 (상품판매/위탁판매 × 매입원가/제조원가 × 소매/도매) =====
                    if all(c in merged_df.columns for c in ['매입원가_누적합계', '제조원가_누적합계']):
                        # 매입원가 / 제조원가 각각 피벗 (상품/위탁 × 소/도매 × 판매월)
                        def _pivot_cost(col_name):
                            p = merged_df.pivot_table(
                                index=['상품/위탁', '소/도매'], columns='판매월',
                                values=col_name, aggfunc='sum', fill_value=0, observed=False,
                            )
                            p = p.reindex(columns=range(1, 13), fill_value=0)
                            p['연간 총합'] = p.sum(axis=1)
                            return p

                        cost_purchase = _pivot_cost('매입원가_누적합계')   # 매입원가
                        cost_mfg = _pivot_cost('제조원가_누적합계')         # 제조원가

                        # 계층 행 구성
                        # 상품판매 / (매입원가) / 상품×소매 / 상품×도매 / (제조원가) / 상품×소매 / 상품×도매
                        # 위탁판매 / (매입원가) / 위탁×소매 / 위탁×도매 / (제조원가) / 위탁×소매 / 위탁×도매
                        # 합계
                        rows = []  # (label_a, label_b, label_c, 값 시리즈)
                        for big in ['상품', '위탁']:
                            big_label = '상품판매' if big == '상품' else '위탁판매'
                            # 매입원가 소계
                            sub_purchase_소 = cost_purchase.loc[(big, '소매')] if (big, '소매') in cost_purchase.index else pd.Series(0, index=cost_purchase.columns)
                            sub_purchase_도 = cost_purchase.loc[(big, '도매')] if (big, '도매') in cost_purchase.index else pd.Series(0, index=cost_purchase.columns)
                            sub_purchase = sub_purchase_소 + sub_purchase_도

                            sub_mfg_소 = cost_mfg.loc[(big, '소매')] if (big, '소매') in cost_mfg.index else pd.Series(0, index=cost_mfg.columns)
                            sub_mfg_도 = cost_mfg.loc[(big, '도매')] if (big, '도매') in cost_mfg.index else pd.Series(0, index=cost_mfg.columns)
                            sub_mfg = sub_mfg_소 + sub_mfg_도

                            big_total = sub_purchase + sub_mfg

                            rows.append((big_label, '', '', big_total))
                            rows.append(('', '매입원가', '', sub_purchase))
                            rows.append(('', '', '소매', sub_purchase_소))
                            rows.append(('', '', '도매', sub_purchase_도))
                            rows.append(('', '제조원가', '', sub_mfg))
                            rows.append(('', '', '소매', sub_mfg_소))
                            rows.append(('', '', '도매', sub_mfg_도))

                        # 전체 합계
                        total_series = sum(r[3] for r in rows if r[0] in ('상품판매', '위탁판매'))
                        rows.append(('합계', '', '', total_series))

                        # 분모 (대당) — 첫 번째 표 s_p 기반: 상품/위탁 × 소/도매 의 대수
                        def _qty(big, sub=None):
                            """상품/위탁 × 소/도매 의 판매대수 시리즈 (12개월 + 연간 총합)."""
                            if sub is None:
                                # 상품 전체 = 상품 소매 + 상품 도매
                                q_소 = s_p.loc[(big, '소매')] if (big, '소매') in s_p.index else pd.Series(0, index=s_p.columns)
                                q_도 = s_p.loc[(big, '도매')] if (big, '도매') in s_p.index else pd.Series(0, index=s_p.columns)
                                return q_소 + q_도
                            else:
                                return s_p.loc[(big, sub)] if (big, sub) in s_p.index else pd.Series(0, index=s_p.columns)

                        # 각 행의 분모(대수) 매핑
                        qty_map = []
                        idx_pos = 0
                        for big in ['상품', '위탁']:
                            qty_big = _qty(big)
                            qty_소 = _qty(big, '소매')
                            qty_도 = _qty(big, '도매')
                            qty_map.extend([qty_big, qty_big, qty_소, qty_도, qty_big, qty_소, qty_도])
                        # 합계 행 분모 = 상품 전체 + 위탁 전체
                        qty_map.append(_qty('상품') + _qty('위탁'))

                        # 표시 방식: 기본은 금액만, 토글 ON 은 대당 금액만.
                        def _fmt_one_line(amount, qty):
                            a = int(round(float(amount))) if pd.notna(amount) else 0
                            q = float(qty) if pd.notna(qty) else 0
                            per = int(round(a / q)) if q else 0
                            if show_per_unit_only:
                                return '-' if per == 0 else f"{per:,}"
                            return '-' if a == 0 else f"{a:,}"

                        # 표 만들기 (중복 인덱스 라벨 → zero-width space 로 고유화, Styler 호환)
                        raw_tuples = [(r[0], r[1], r[2]) for r in rows]
                        seen = {}
                        unique_tuples = []
                        for t in raw_tuples:
                            count = seen.get(t, 0)
                            seen[t] = count + 1
                            if count > 0:
                                # 보이지 않는 zero-width space 를 끝에 붙여 고유화
                                t = tuple(v + ("\u200b" * count) if i == 2 else v for i, v in enumerate(t))
                            unique_tuples.append(t)
                        cost_idx = pd.MultiIndex.from_tuples(unique_tuples)
                        cost_columns = cost_purchase.columns
                        cost_df = pd.DataFrame(index=cost_idx, columns=cost_columns, dtype=object)
                        for i, (r, q_series) in enumerate(zip(rows, qty_map)):
                            value_series = r[3]
                            for col in cost_columns:
                                cost_df.iat[i, cost_df.columns.get_loc(col)] = _fmt_one_line(
                                    value_series.get(col, 0), q_series.get(col, 0)
                                )

                        # 강조행: 대분류(상품판매/위탁판매/합계) 위치 (raw_tuples 기준)
                        highlight_positions = set(
                            i for i, t in enumerate(raw_tuples) if t[0] in ('상품판매', '위탁판매', '합계')
                        )

                        flat_cost_df = cost_df.reset_index(drop=True)
                        flat_cost_df.insert(0, '구분', _flatten_sparse_labels(raw_tuples))
                        _render_month_table(
                            "월별 매출원가", _amount_caption("대당"), flat_cost_df, highlight_positions,
                        )

                        # ===== 월별 매출총이익 = 월별 매출 - 월별 매출원가 =====
                        # 각 (상품/위탁, 소/도매) 별로 매출 - 매출원가 직접 계산
                        def _get_series(p, big, sub):
                            """피벗 p 에서 (big, sub) 행 시리즈 반환, 없으면 0."""
                            if (big, sub) in p.index:
                                return p.loc[(big, sub)]
                            return pd.Series(0, index=p.columns)

                        # 계층 행 구성 (2단계: 대분류 → 소매/도매)
                        gp_rows = []
                        for big in ['상품', '위탁']:
                            big_label = '상품판매' if big == '상품' else '위탁판매'
                            # 매출 - 매출원가 (소매/도매 각각)
                            rev_소 = _get_series(r_p, big, '소매')
                            rev_도 = _get_series(r_p, big, '도매')
                            cost_소 = _get_series(cost_purchase, big, '소매') + _get_series(cost_mfg, big, '소매')
                            cost_도 = _get_series(cost_purchase, big, '도매') + _get_series(cost_mfg, big, '도매')
                            gp_소 = rev_소 - cost_소
                            gp_도 = rev_도 - cost_도
                            gp_big = gp_소 + gp_도
                            gp_rows.append((big_label, '', gp_big))
                            gp_rows.append(('', '소매', gp_소))
                            gp_rows.append(('', '도매', gp_도))

                        # 합계
                        total_gp = sum(r[2] for r in gp_rows if r[0] in ('상품판매', '위탁판매'))
                        gp_rows.append(('합계', '', total_gp))

                        # 분모(대수) 매핑
                        gp_qty_map = []
                        for big in ['상품', '위탁']:
                            qty_big = _qty(big)
                            qty_소 = _qty(big, '소매')
                            qty_도 = _qty(big, '도매')
                            gp_qty_map.extend([qty_big, qty_소, qty_도])
                        gp_qty_map.append(_qty('상품') + _qty('위탁'))

                        # 인덱스 고유화
                        raw_gp = [(r[0], r[1]) for r in gp_rows]
                        seen_gp = {}
                        unique_gp = []
                        for t in raw_gp:
                            c = seen_gp.get(t, 0)
                            seen_gp[t] = c + 1
                            if c > 0:
                                t = tuple(v + ("\u200b" * c) if i == 1 else v for i, v in enumerate(t))
                            unique_gp.append(t)
                        gp_idx = pd.MultiIndex.from_tuples(unique_gp)
                        gp_df = pd.DataFrame(index=gp_idx, columns=r_p.columns, dtype=object)
                        for i, (r, q_series) in enumerate(zip(gp_rows, gp_qty_map)):
                            value_series = r[2]
                            for col in r_p.columns:
                                gp_df.iat[i, gp_df.columns.get_loc(col)] = _fmt_one_line(
                                    value_series.get(col, 0), q_series.get(col, 0)
                                )

                        # 강조행: 상품판매/위탁판매/합계
                        gp_highlight = set(
                            i for i, t in enumerate(raw_gp) if t[0] in ('상품판매', '위탁판매', '합계')
                        )
                        flat_gp_df = gp_df.reset_index(drop=True)
                        flat_gp_df.insert(0, '구분', _flatten_sparse_labels(raw_gp))
                        _render_month_table(
                            "월별 매출총이익", _amount_caption("대당 매출총이익"), flat_gp_df, gp_highlight,
                        )

                        # ===== 월별 매출총이익률 = 매출총이익 / 매출 × 100 (%) =====
                        # 매출총이익 표(gp_rows)의 값들을 재사용해서 매출(rev) 로 나눔
                        gpr_rows = []
                        for big in ['상품', '위탁']:
                            big_label = '상품판매' if big == '상품' else '위탁판매'
                            rev_소 = _get_series(r_p, big, '소매')
                            rev_도 = _get_series(r_p, big, '도매')
                            cost_소 = _get_series(cost_purchase, big, '소매') + _get_series(cost_mfg, big, '소매')
                            cost_도 = _get_series(cost_purchase, big, '도매') + _get_series(cost_mfg, big, '도매')
                            gp_소 = rev_소 - cost_소
                            gp_도 = rev_도 - cost_도
                            rev_big = rev_소 + rev_도
                            gp_big = gp_소 + gp_도
                            gpr_rows.append((big_label, '', gp_big, rev_big))
                            gpr_rows.append(('', '소매', gp_소, rev_소))
                            gpr_rows.append(('', '도매', gp_도, rev_도))

                        # 합계
                        total_rev = sum(r[3] for r in gpr_rows if r[0] in ('상품판매', '위탁판매'))
                        total_gp_ = sum(r[2] for r in gpr_rows if r[0] in ('상품판매', '위탁판매'))
                        gpr_rows.append(('합계', '', total_gp_, total_rev))

                        # 비율 포맷: "12.5%" (매출=0 이면 "-")
                        def _fmt_ratio(gp_val, rev_val):
                            g = float(gp_val) if pd.notna(gp_val) else 0.0
                            r_ = float(rev_val) if pd.notna(rev_val) else 0.0
                            if r_ == 0:
                                return '-'
                            return f"{g / r_ * 100:.1f}%"

                        # 인덱스 고유화
                        raw_gpr = [(r[0], r[1]) for r in gpr_rows]
                        seen_gpr = {}
                        unique_gpr = []
                        for t in raw_gpr:
                            c = seen_gpr.get(t, 0)
                            seen_gpr[t] = c + 1
                            if c > 0:
                                t = tuple(v + ("\u200b" * c) if i == 1 else v for i, v in enumerate(t))
                            unique_gpr.append(t)
                        gpr_idx = pd.MultiIndex.from_tuples(unique_gpr)
                        gpr_df = pd.DataFrame(index=gpr_idx, columns=r_p.columns, dtype=object)
                        for i, r in enumerate(gpr_rows):
                            gp_series, rev_series = r[2], r[3]
                            for col in r_p.columns:
                                gpr_df.iat[i, gpr_df.columns.get_loc(col)] = _fmt_ratio(
                                    gp_series.get(col, 0), rev_series.get(col, 0)
                                )

                        # 강조행
                        gpr_highlight = set(
                            i for i, t in enumerate(raw_gpr) if t[0] in ('상품판매', '위탁판매', '합계')
                        )
                        flat_gpr_df = gpr_df.reset_index(drop=True)
                        flat_gpr_df.insert(0, '구분', _flatten_sparse_labels(raw_gpr))
                        _render_month_table(
                            "월별 매출총이익률", "(매출총이익 ÷ 매출 × 100)", flat_gpr_df, gpr_highlight,
                        )

                st.divider()

            # 차량별 손익현황 (제목 → 기간 필터 → 표)
            st.markdown(
                """
                <div style="display:flex; justify-content:space-between; align-items:flex-end;">
                    <div style="font-size:20px; font-weight:bold;">차량별 손익현황</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # 판매연도 / 판매월 기간 선택 필터 (시작 ~ 끝)
            year_col = _find_column(merged_df, ["판매년도", "판매연도", "매출년도", "매출연도"])
            month_col = _find_column(merged_df, ["판매월", "매출월", "월"])

            filtered_df = merged_df
            start_key = end_key = None
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

            # 엑셀 다운로드 (원본 파일·기간 선택이 안 바뀌면 재생성하지 않음)
            def _build_vehicle_pl_excel_bytes():
                from io import BytesIO
                buf = BytesIO()
                with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                    filtered_df.to_excel(writer, index=False, sheet_name="차량별 손익현황")
                return buf.getvalue()

            _master_mtime = os.path.getmtime(master_file) if os.path.exists(master_file) else None
            st.download_button(
                "차량별 손익현황 다운로드",
                data=_memoize(
                    "vehicle_pl_download",
                    (_master_mtime, len(filtered_df), start_key, end_key),
                    _build_vehicle_pl_excel_bytes,
                ),
                file_name="차량별_손익현황.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

            st.dataframe(filtered_df)
    else:
        st.warning("매출, 원가파일이 비어 있습니다.")

