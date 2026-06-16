"""손익분석 (Cost Summary) - Streamlit 페이지

구성:
    1. 유틸 (DataFrame 표시용)
    2. 결산 연/월 선택
    3. 기초 DB 업로드
    4. 매입원가 업로드
    5. 제조원가 업로드
    6. 최종 원가 렌더링
    7. 페이지 엔트리
"""

import re

import numpy as np
import pandas as pd
import streamlit as st
from datetime import datetime

from cost_summary_preprocess import (
    BASE_DF_KEYS,
    HIDDEN_BASE_DF_KEYS,
    PRODUCT_ID_COLUMNS,
    collect_product_ids,
    dataframe_for_display,
    dataframe_to_excel_bytes,
    filter_purchase_inquiry,
    preprocess_combined_manufacturing_cost_files,
    preprocess_consignment_ledger,
    preprocess_cost_file,
    preprocess_direct_expense_file,
    preprocess_material_cost_file,
    preprocess_opening_inventory,
    preprocess_payback_file,
    preprocess_product_ledger,
    preprocess_product_master,
    preprocess_purchase_inquiry,
    preprocess_sales,
    preprocess_waste_resource_file,
    workbook_to_excel_bytes,
)
from cost_summary_builder import build_final_cost_df, get_last_manufacturing_expense_diagnostics


def _build_input_fingerprint(*dfs_and_values):
    """빌드 입력의 지문 생성 (shape + 숫자합계 + 스칼라값).

    shape 뿐 아니라 숫자 컬럼 합계도 포함해, 행수가 같고 값만 바뀐 경우도 감지.
    """
    def df_signature(df):
        try:
            numeric_sum = float(
                pd.to_numeric(
                    df.select_dtypes(include="number").sum().sum(), errors="coerce"
                )
            )
        except Exception:
            numeric_sum = 0.0
        return (df.shape, tuple(map(str, df.columns)), round(numeric_sum, 2))

    parts = []
    for item in dfs_and_values:
        if isinstance(item, pd.DataFrame):
            parts.append(("df", df_signature(item)))
        elif isinstance(item, dict):
            sub = []
            for k, v in sorted(item.items(), key=lambda x: str(x[0])):
                if isinstance(v, pd.DataFrame):
                    sub.append((str(k), df_signature(v)))
                else:
                    sub.append((str(k), str(type(v))))
            parts.append(("dict", tuple(sub)))
        else:
            parts.append(("val", str(item)))
    return repr(parts)


def _cached_build_final_cost_df(
    product_id_df, purchase_cost_sheet_dfs, inventory_df, settlement_month,
    manufacturing_cost_sheet_dfs, verification_sheets, settlement_year,
    cost_driver_dfs, combined_cost_driver_df, consignment_ledger_df,
):
    """입력 지문이 같으면 session_state 에 저장된 직전 빌드 결과를 재사용."""
    fingerprint = _build_input_fingerprint(
        product_id_df, purchase_cost_sheet_dfs, inventory_df, settlement_month,
        manufacturing_cost_sheet_dfs, verification_sheets, settlement_year,
        cost_driver_dfs, combined_cost_driver_df, consignment_ledger_df,
    )
    cache_key = "_final_cost_cache"
    cached = st.session_state.get(cache_key)
    if cached is not None and cached.get("fingerprint") == fingerprint:
        # 진단도 함께 복원 (모듈 백업에 다시 세팅)
        return cached["result"], cached.get("diagnostics", [])

    result = build_final_cost_df(
        product_id_df,
        purchase_cost_sheet_dfs,
        inventory_df,
        settlement_month,
        manufacturing_cost_sheet_dfs=manufacturing_cost_sheet_dfs,
        verification_sheets=verification_sheets,
        settlement_year=settlement_year,
        cost_driver_dfs=cost_driver_dfs,
        combined_cost_driver_df=combined_cost_driver_df,
        consignment_ledger_df=consignment_ledger_df,
    )
    diagnostics = get_last_manufacturing_expense_diagnostics()
    st.session_state[cache_key] = {
        "fingerprint": fingerprint,
        "result": result,
        "diagnostics": diagnostics,
    }
    return result, diagnostics


# ============================================================
# 1. 유틸
# ============================================================

def empty_product_id_df():
    return pd.DataFrame(columns=PRODUCT_ID_COLUMNS)


def initialize_base_dfs():
    return {key: None for key in BASE_DF_KEYS}


def _unique_sheet_label(sheet_dfs, label):
    label = str(label).strip() or "Sheet"
    unique_label = label
    index = 2
    while unique_label in sheet_dfs:
        unique_label = f"{label}_{index}"
        index += 1
    return unique_label


def render_dataframe_tabs(sheet_dfs):
    tabs = st.tabs(list(sheet_dfs.keys()))
    for i, sheet_name in enumerate(sheet_dfs.keys()):
        with tabs[i]:
            current_df = sheet_dfs[sheet_name]
            st.write(f"건수: {len(current_df):,}건")
            st.dataframe(dataframe_for_display(current_df), use_container_width=True)


def render_sheet_workbook(sheet_dfs, download_label, file_name, empty_message):
    if not sheet_dfs:
        st.info(empty_message)
        return
    st.download_button(
        download_label,
        data=workbook_to_excel_bytes(sheet_dfs),
        file_name=file_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    render_dataframe_tabs(sheet_dfs)


# ============================================================
# 2. 결산 연/월 선택
# ============================================================

def render_settlement_selector():
    if "settlement_year" not in st.session_state:
        st.session_state["settlement_year"] = pd.Timestamp.today().year
    if "settlement_month" not in st.session_state:
        st.session_state["settlement_month"] = pd.Timestamp.today().month

    year_col, month_col, _, button_col = st.columns([1, 1, 1, 0.6])

    with year_col:
        selected_year = st.number_input(
            "결산연도", min_value=2000, max_value=2100,
            value=int(st.session_state["settlement_year"]),
            step=1, key="selected_settlement_year",
        )
    with month_col:
        selected_month = st.selectbox(
            "결산월", options=list(range(1, 13)),
            index=st.session_state["settlement_month"] - 1,
            format_func=lambda month: f"{month}월",
            key="selected_settlement_month",
        )
    with button_col:
        st.write("")
        apply_period = st.button(
            "결산연/월 적용", key="apply_settlement_period", use_container_width=True,
        )

    if apply_period:
        st.session_state["settlement_year"] = int(selected_year)
        st.session_state["settlement_month"] = selected_month
        st.success(f"결산연/월이 {int(selected_year)}년 {selected_month}월로 지정되었습니다.")

    settlement_year = st.session_state["settlement_year"]
    settlement_month = st.session_state["settlement_month"]
    st.caption(f"현재 결산연/월: {settlement_year}년 {settlement_month}월")
    st.divider()
    return settlement_year, settlement_month


# ============================================================
# 3. 기초 DB 업로드
# ============================================================

def process_base_file(fname, file, dfs, settlement_month):
    """파일명에 따라 기초 DB 전처리 라우팅."""
    if "매입조회" in fname:
        dfs["매입조회"] = preprocess_purchase_inquiry(file)
    elif "전체상품조회" in fname:
        dfs["전체상품조회"] = preprocess_product_master(file)
    elif "위탁수불부" in fname:
        ledger_all, ledger_opening, ledger_inbound = preprocess_consignment_ledger(
            file, settlement_month
        )
        dfs["위탁수불부"] = ledger_all
        dfs["위탁수불부_전체"] = ledger_all
        dfs["위탁수불부_기초"] = ledger_opening
        dfs["위탁수불부_입고"] = ledger_inbound
    elif "검사매출" in fname:
        dfs["검사매출"] = preprocess_sales(file)
    elif "정비매출" in fname:
        dfs["정비매출"] = preprocess_sales(file)


def process_opening_inventory_files(file_map, dfs):
    """기초재고 파일 처리 (매입조회 필터링 포함)."""
    for fname, file in file_map.items():
        if "기초재고" not in fname:
            continue
        try:
            inventory_all, inventory_filtered = preprocess_opening_inventory(
                file, dfs["매입조회"]
            )
            dfs["기초재고_전체"] = inventory_all
            dfs["기초재고"] = inventory_filtered
            dfs["매입조회"] = filter_purchase_inquiry(dfs["매입조회"], dfs["기초재고_전체"])
        except Exception as exc:
            st.error(f"{fname} 처리 중 오류: {exc}")


def render_base_upload(settlement_year, settlement_month):
    st.subheader("1 원가대상")

    uploaded_files = st.file_uploader(
        "원가대상 업로드(기초재고/매입조회/전체상품조회/위탁수불부/복수비용_검사/복수비용_정비)", type=["xlsx"], accept_multiple_files=True,
    )

    dfs = initialize_base_dfs()
    product_id_df = empty_product_id_df()

    if uploaded_files:
        file_map = {file.name: file for file in uploaded_files}

        for fname, file in file_map.items():
            try:
                process_base_file(fname, file, dfs, settlement_month)
            except Exception as exc:
                st.error(f"{fname} 처리 중 오류: {exc}")

        process_opening_inventory_files(file_map, dfs)

    # 기초재고가 비어있으면 누적 마스터에서 자동 생성 (직전월 기말_수량==1)
    inventory_now = dfs.get("기초재고")
    if inventory_now is None or (
        isinstance(inventory_now, pd.DataFrame) and inventory_now.empty
    ):
        try:
            from cost_summary_builder import _build_inventory_df_from_master
            auto_inv = _build_inventory_df_from_master(
                settlement_year, settlement_month,
            )
            if auto_inv is not None and not auto_inv.empty:
                dfs["기초재고"] = auto_inv
                dfs["기초재고_전체"] = auto_inv
                prev_y = settlement_year - 1 if settlement_month == 1 else settlement_year
                prev_m = 12 if settlement_month == 1 else settlement_month - 1
                st.info(
                    f"기초재고 파일이 업로드되지 않아 누적 마스터에서 "
                    f"{prev_y}-{prev_m:02d} 기말 데이터를 자동으로 가져왔습니다. "
                    f"({len(auto_inv):,}건)"
                )
        except Exception as exc:
            st.warning(f"기초재고 자동 생성 중 오류: {exc}")

    if uploaded_files or (
        isinstance(dfs.get("기초재고"), pd.DataFrame) and not dfs["기초재고"].empty
    ):
        st.divider()
        st.subheader("- 상품ID 확인")
        product_id_df = collect_product_ids(dfs, settlement_year, settlement_month)

        if not product_id_df.empty:
            st.write(f"데이터 건수: {len(product_id_df):,}건")
            with st.expander("구분 포함 상세 보기"):
                st.download_button(
                    "엑셀 다운로드",
                    data=dataframe_to_excel_bytes(product_id_df, sheet_name="구분포함상세"),
                    file_name="product_id_detail.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                st.dataframe(dataframe_for_display(product_id_df), use_container_width=True)
        else:
            st.info("상품ID를 가진 업로드 데이터가 아직 없습니다.")

    visible_dfs = {
        key: value
        for key, value in dfs.items()
        if key not in HIDDEN_BASE_DF_KEYS and value is not None
    }
    if visible_dfs:
        with st.expander("파일별 개별 데이터 확인"):
            render_dataframe_tabs(visible_dfs)

    return dfs, product_id_df


# ============================================================
# 4. 매입원가 업로드
# ============================================================

def preprocess_purchase_cost_files(
    uploaded_cost_files, product_id_df, dfs, settlement_year, settlement_month,
):
    """매입원가 파일들 → {시트라벨: df}."""
    cost_sheet_dfs = {}
    product_ledger_frames = []

    # 상품원장 먼저 처리 (lookup 용)
    for file in uploaded_cost_files:
        if "상품원장" not in file.name:
            continue
        try:
            product_ledger_df = preprocess_product_ledger(
                file, product_id_df, dfs.get("기초재고_전체"),
                settlement_year, settlement_month,
            )
            label = _unique_sheet_label(cost_sheet_dfs, "상품원장")
            cost_sheet_dfs[label] = product_ledger_df
            product_ledger_frames.append(product_ledger_df)
        except Exception as exc:
            st.error(f"{file.name} 처리 중 오류: {exc}")

    product_ledger_lookup_df = (
        pd.concat(product_ledger_frames, ignore_index=True)
        if product_ledger_frames else pd.DataFrame()
    )

    # 그 외 파일 처리
    for file in uploaded_cost_files:
        if "상품원장" in file.name:
            continue
        try:
            file_label = file.name.rsplit(".", 1)[0]

            if "폐자원" in file.name:
                if product_id_df.empty and product_ledger_lookup_df.empty:
                    st.warning("폐자원 파일의 상품ID를 가져오려면 1번 기초 DB 또는 상품원장 파일도 함께 업로드하세요.")
                file_sheets = preprocess_waste_resource_file(
                    file, product_ledger_lookup_df, product_id_df,
                )
                display_label = "폐자원공제"
            elif "페이백" in file.name:
                if product_id_df.empty:
                    st.warning("페이백 파일의 상품ID를 가져오려면 1번 기초 DB 파일도 함께 업로드하세요.")
                file_sheets = preprocess_payback_file(
                    file, product_id_df, settlement_year, settlement_month,
                )
                display_label = "페이백"
            else:
                file_sheets = preprocess_cost_file(file)
                display_label = None

            for sheet_name, df in file_sheets.items():
                output_sheet_name = display_label or f"{file_label}_{sheet_name}"
                cost_sheet_dfs[_unique_sheet_label(cost_sheet_dfs, output_sheet_name)] = df
        except Exception as exc:
            st.error(f"{file.name} 처리 중 오류: {exc}")

    return cost_sheet_dfs


def render_purchase_cost_upload(product_id_df, dfs, settlement_year, settlement_month):
    st.markdown("##### 2-1 매입원가")

    uploaded_cost_files = st.file_uploader(
        "매입원가 업로드(상품원장/재활용폐자원세액공제신고서/페이백)",
        type=["xlsx", "xls"], accept_multiple_files=True, key="cost_files",
    )

    if not uploaded_cost_files:
        return {}
    if len(uploaded_cost_files) > 3:
        st.warning("원가 파일은 3개까지 업로드")

    cost_sheet_dfs = preprocess_purchase_cost_files(
        uploaded_cost_files, product_id_df, dfs, settlement_year, settlement_month,
    )
    render_sheet_workbook(
        cost_sheet_dfs,
        "매입원가 파일 다운로드",
        "purchase_cost_preprocessed.xlsx",
        "매입원가 파일에서 표시할 데이터가 없습니다.",
    )
    return cost_sheet_dfs


# ============================================================
# 5. 제조원가 업로드
# ============================================================

def render_manufacturing_cost_upload(product_id_df, settlement_year, settlement_month):
    st.markdown("##### 2-2 제조원가")

    uploaded_files = st.file_uploader(
        "제조원가 업로드(재료비/노무비/부문별경비/직접경비)",
        type=["xlsx", "xls"], accept_multiple_files=True, key="manufacturing_cost_files",
    )

    manufacturing_cost_sheet_dfs = {}
    if not uploaded_files:
        return manufacturing_cost_sheet_dfs

    # 노무비 / 부문별경비 (여러 파일 통합 처리)
    combined_files_specs = [
        ("노무비", "노무비"),
        ("부문별경비", "제조경비"),
    ]
    for filename_keyword, cost_type in combined_files_specs:
        matching_files = [f for f in uploaded_files if filename_keyword in f.name]
        if not matching_files:
            continue
        try:
            combined_df = preprocess_combined_manufacturing_cost_files(
                matching_files, cost_type, settlement_year, settlement_month,
            )
            label = _unique_sheet_label(manufacturing_cost_sheet_dfs, filename_keyword)
            manufacturing_cost_sheet_dfs[label] = combined_df
        except Exception as exc:
            st.error(f"{filename_keyword} 파일 처리 중 오류: {exc}")

    # 그 외 파일들
    combined_keywords = ("노무비", "부문별경비")
    for file in uploaded_files:
        if any(keyword in file.name for keyword in combined_keywords):
            continue
        try:
            file_label = file.name.rsplit(".", 1)[0]

            if "재료비" in file.name:
                file_sheets = preprocess_material_cost_file(file, product_id_df)
                display_label = "재료비"
            elif "직접경비" in file.name:
                file_sheets = preprocess_direct_expense_file(file, product_id_df)
                display_label = "직접경비"
            else:
                file_sheets = preprocess_cost_file(file)
                display_label = None

            for sheet_name, df in file_sheets.items():
                output_sheet_name = display_label or f"{file_label}_{sheet_name}"
                manufacturing_cost_sheet_dfs[
                    _unique_sheet_label(manufacturing_cost_sheet_dfs, output_sheet_name)
                ] = df
        except Exception as exc:
            st.error(f"{file.name} 처리 중 오류: {exc}")

    render_sheet_workbook(
        manufacturing_cost_sheet_dfs,
        "제조원가 파일 다운로드",
        "manufacturing_cost_preprocessed.xlsx",
        "제조원가 파일에서 표시할 데이터가 없습니다.",
    )
    return manufacturing_cost_sheet_dfs


# ============================================================
# 6. 원가동인 업로드
# ============================================================

# 키워드 매칭 우선순위 (긴/명확한 것부터 — 짧은 'sm'이 다른 이름에 잘못 매칭되지 않도록)
COST_DRIVER_KEYWORDS = ["AQI실적", "RTLS", "rtc", "TS", "sm"]

# 키워드별 시트 선택 규칙
#   "settlement": 시트명이 결산연도-월에 매칭되는 시트만 사용 (예: '2026-01')
#   "all":        모든 시트 사용
#   prefix 문자열: 시트명이 해당 prefix 로 시작하는 시트만 사용
COST_DRIVER_SHEET_RULE = {
    "rtc": "settlement",
    "sm": "settlement",
    "RTLS": "품질개선_RTLS",
    "TS": "all",
    "AQI실적": "all",
}

# 결산연도-월 시트명 후보를 만드는 헬퍼 (rtc, sm 등 prefix 가 없는 경우 사용)
def _build_settlement_sheet_name_candidates(settlement_year, settlement_month):
    if settlement_year is None or settlement_month is None:
        return set()
    year = int(settlement_year)
    month = int(settlement_month)
    raw = {
        f"{year}-{month:02d}", f"{year}-{month}",
        f"{year}/{month:02d}", f"{year}/{month}",
        f"{year}.{month:02d}", f"{year}.{month}",
        f"{year}{month:02d}",
        f"{year}년 {month}월", f"{year}년{month}월",
        f"{year}년 {month:02d}월",
        f"{month}월", f"{month:02d}월",
        str(month), f"{month:02d}",
    }
    return {re.sub(r"\s+", "", c) for c in raw}


def _sheet_matches_settlement(sheet_name, candidates, settlement_year, settlement_month):
    """결산월 시트명 매칭.

    1) 정규화된 시트명 전체가 후보와 동일하면 매칭
    2) 시트명이 'YYYY-MM' 또는 'YYYY-M' 형식으로 시작하면 매칭 (예: '2026-01_상세')
    """
    normalized = re.sub(r"\s+", "", str(sheet_name).strip())
    if normalized in candidates:
        return True

    if settlement_year is not None and settlement_month is not None:
        year = int(settlement_year)
        month = int(settlement_month)
        # 'YYYY-MM' 또는 'YYYY-M' 으로 시작
        for prefix in (f"{year}-{month:02d}", f"{year}-{month}"):
            if normalized.startswith(prefix):
                return True

    return False


def _match_cost_driver_keyword(filename):
    """파일명에서 원가동인 키워드 우선순위대로 매칭."""
    lower_name = filename.lower()
    for keyword in COST_DRIVER_KEYWORDS:
        if keyword.lower() in lower_name:
            return keyword
    return None


def render_cost_driver_upload(settlement_year=None, settlement_month=None):
    st.subheader("3 배부 기준자료")
    st.markdown("##### 3-1 원가동인")

    uploaded_files = st.file_uploader(
        "원가동인 업로드 (AQI실적/TS/RTLS/RTC/SM)",
        type=["xlsx", "xls"], accept_multiple_files=True, key="cost_driver_files",
    )

    cost_driver_dfs = {}
    if not uploaded_files:
        return cost_driver_dfs

    settlement_candidates = _build_settlement_sheet_name_candidates(
        settlement_year, settlement_month,
    )

    for file in uploaded_files:
        matched_keyword = _match_cost_driver_keyword(file.name)
        if matched_keyword is None:
            st.warning(
                f"{file.name}: AQI실적, TS, RTLS, RTC, SM 중 하나가 파일명에 포함되어야 합니다."
            )
            continue

        try:
            sheets = pd.read_excel(file, sheet_name=None)
        except Exception as exc:
            st.error(f"{file.name} 처리 중 오류: {exc}")
            continue

        sheet_rule = COST_DRIVER_SHEET_RULE.get(matched_keyword, "all")
        all_sheet_names = list(sheets.keys())  # 디버그용

        cleaned_sheets = {}
        for sheet_name, df in sheets.items():
            # 시트 선택 규칙
            if sheet_rule == "all":
                pass  # 모든 시트 사용
            elif sheet_rule == "settlement":
                if not settlement_candidates:
                    pass  # 결산월 미지정 시 모든 시트 사용
                elif not _sheet_matches_settlement(
                    sheet_name, settlement_candidates,
                    settlement_year, settlement_month,
                ):
                    continue
            else:
                # prefix 문자열
                if not str(sheet_name).startswith(sheet_rule):
                    continue

            df.columns = [str(c).strip() for c in df.columns]
            df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
            if df.empty:
                continue
            # rtc 파일은 '상품아이디' 컬럼을 '상품ID' 로 통일
            if "상품ID" not in df.columns and "상품아이디" in df.columns:
                df = df.rename(columns={"상품아이디": "상품ID"})
            cleaned_sheets[sheet_name] = df

        if cleaned_sheets:
            cost_driver_dfs[matched_keyword] = cleaned_sheets
            st.success(
                f"✅ {file.name} → [{matched_keyword}] "
                f"선택된 시트: {list(cleaned_sheets.keys())}"
            )
        else:
            if sheet_rule == "settlement" and settlement_candidates:
                st.warning(
                    f"⚠️ {file.name}: 결산연도-월"
                    f"({settlement_year}-{int(settlement_month):02d}) "
                    f"에 해당하는 시트를 찾을 수 없습니다. "
                    f"파일에 있는 시트: {all_sheet_names}"
                )
            elif isinstance(sheet_rule, str) and sheet_rule not in ("all", "settlement"):
                st.warning(
                    f"⚠️ {file.name}: 시트명이 '{sheet_rule}' 로 시작하는 시트가 없습니다. "
                    f"파일에 있는 시트: {all_sheet_names}"
                )

    if cost_driver_dfs:
        with st.expander("원가동인 데이터 확인 (선택된 시트만 표시)"):
            flattened = {
                f"{keyword} / {sheet_name}": df
                for keyword, sheet_map in cost_driver_dfs.items()
                for sheet_name, df in sheet_map.items()
            }
            render_dataframe_tabs(flattened)

    return cost_driver_dfs


# ============================================================
# 원가동인 통합 (AQI실적 / TS / RTLS → 공정 통합 DataFrame)
# ============================================================

# 통합 DataFrame 컬럼 순서
COMBINED_DRIVER_COLUMNS = [
    "공정", "차량번호", "모델명",
    "최초측정일", "최초측정시간", "최종측정일", "최종측정시간",
    "담당자",
]

# 구분 변환 규칙 (공정 → 구분)
PROCESS_TO_CATEGORY = {
    "TU": "정비",
    "PL": "판금",
    "PA": "도장",
}


def _pick_first_existing(df, candidates, default=""):
    """후보 컬럼 중 가장 먼저 존재하는 것의 Series 반환. 없으면 default."""
    for column in candidates:
        if column in df.columns:
            return df[column]
    return pd.Series([default] * len(df), index=df.index)


def _normalize_rtls_sheet(df, sheet_name):
    """RTLS 시트 → 통합 컬럼 매핑 (공정은 원본 '공정' 컬럼 값)."""
    process_value = _pick_first_existing(df, ["공정"], default=sheet_name)
    return pd.DataFrame({
        "공정": process_value,
        "차량번호": _pick_first_existing(df, ["차량번호"]),
        "모델명": _pick_first_existing(df, ["차량모델", "모델명"]),
        "최초측정일": _pick_first_existing(df, ["최초측정일"]),
        "최초측정시간": _pick_first_existing(df, ["최초측정시간"]),
        "최종측정일": _pick_first_existing(df, ["최종측정일"]),
        "최종측정시간": _pick_first_existing(df, ["최종측정시간"]),
        "담당자": _pick_first_existing(df, ["작업자", "담당자"]),
    })


def _normalize_ts_sheet(df, sheet_name):
    """TS 시트 → 통합 컬럼 매핑 (공정은 'TS' 고정).
    TS 컬럼: 차량번호, 차량명, 검사일(20260102), (빈값), 검사일, (빈값), 검사자
    """
    inspection_date = _pick_first_existing(df, ["검사일"])
    return pd.DataFrame({
        "공정": "TS",
        "차량번호": _pick_first_existing(df, ["차량번호"]),
        "모델명": _pick_first_existing(df, ["차량명", "모델명"]),
        "최초측정일": inspection_date,
        "최초측정시간": "",
        "최종측정일": inspection_date,
        "최종측정시간": "",
        "담당자": _pick_first_existing(df, ["검사자", "담당자"]),
    })


def _normalize_aqi_sheet(df, sheet_name):
    """AQI실적 시트 → 통합 컬럼 매핑 (공정은 시트명, 단 'AQI' → 'RQI' 로 변환).
    AQI 컬럼: 차량번호, 차명, 작업일자, 작업시작시간, 작업일자, 작업종료시간, 담당자
    """
    process_value = str(sheet_name).replace("AQI", "RQI")
    return pd.DataFrame({
        "공정": process_value,
        "차량번호": _pick_first_existing(df, ["차량번호"]),
        "모델명": _pick_first_existing(df, ["차명", "모델명"]),
        "최초측정일": _pick_first_existing(df, ["작업일자"]),
        "최초측정시간": _pick_first_existing(df, ["작업시작시간"]),
        "최종측정일": _pick_first_existing(df, ["작업일자"]),
        "최종측정시간": _pick_first_existing(df, ["작업종료시간"]),
        "담당자": _pick_first_existing(df, ["담당자"]),
    })


def _normalize_process_to_category(process_value):
    """공정 값을 구분(정비/판금/도장)으로 변환. 매핑에 없으면 원값 그대로."""
    text = "" if pd.isna(process_value) else str(process_value).strip()
    return PROCESS_TO_CATEGORY.get(text, text)


# 영업시간 (일별)
WORK_START_HOUR = 8
WORK_START_MINUTE = 30
WORK_END_HOUR = 17
WORK_END_MINUTE = 30


def _parse_date_value(value):
    """다양한 형식의 날짜 → pandas Timestamp (실패 시 NaT).

    지원: datetime, '2026-01-02', '20260102' (8자리), 20260102 (정수) 등
    """
    if pd.isna(value) or value == "":
        return pd.NaT

    # 8자리 정수/문자 형태 ('20260102' 또는 20260102)
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return pd.to_datetime(text, format="%Y%m%d", errors="coerce")

    return pd.to_datetime(value, errors="coerce")


def _parse_time_value(value):
    """시간 문자열/객체 → datetime.time (실패 시 None).

    지원: '09:00', '09:00:00', datetime.time, datetime, pandas Timestamp 등
    """
    if pd.isna(value) or value == "":
        return None

    # 이미 time 객체
    if hasattr(value, "hour") and hasattr(value, "minute") and not hasattr(value, "year"):
        return value
    # datetime / Timestamp
    if hasattr(value, "hour") and hasattr(value, "year"):
        return value.time()

    text = str(value).strip()
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.notna(parsed):
        return parsed.time()
    return None


# 한국 공휴일 백업 (holidays 패키지 미설치 환경용)
# 필요 시 매년 갱신
KOREAN_HOLIDAYS_FALLBACK = {
    2025: [
        "2025-01-01", "2025-01-27", "2025-01-28", "2025-01-29", "2025-01-30",
        "2025-03-01", "2025-03-03", "2025-05-05", "2025-05-06", "2025-06-03",
        "2025-06-06", "2025-08-15", "2025-10-03", "2025-10-06", "2025-10-07",
        "2025-10-08", "2025-10-09", "2025-12-25",
    ],
    2026: [
        "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-03-01",
        "2026-03-02", "2026-05-05", "2026-05-24", "2026-05-25", "2026-06-03",
        "2026-06-06", "2026-07-17", "2026-08-15", "2026-08-17", "2026-09-24",
        "2026-09-25", "2026-09-26", "2026-10-03", "2026-10-05", "2026-10-09",
        "2026-12-25",
    ],
    2027: [
        "2027-01-01", "2027-02-06", "2027-02-07", "2027-02-08", "2027-02-09",
        "2027-03-01", "2027-05-05", "2027-05-13", "2027-06-06", "2027-06-07",
        "2027-08-15", "2027-08-16", "2027-09-14", "2027-09-15", "2027-09-16",
        "2027-10-03", "2027-10-04", "2027-10-09", "2027-10-11", "2027-12-25",
    ],
}


@st.cache_data(show_spinner=False)
def _build_korean_holiday_checker(year_hint=None):
    """한국 공휴일 set (datetime.date 집합) 반환.

    1순위: holidays 패키지 사용
    2순위: KOREAN_HOLIDAYS_FALLBACK 하드코딩 데이터 사용
    (결산연도만 입력이라 캐싱 안전 — 매 리런마다 재계산 방지)
    """
    if year_hint is None:
        year_hint = pd.Timestamp.today().year
    year = int(year_hint)
    years = {year - 1, year, year + 1}

    # 1순위: holidays 패키지
    try:
        import holidays
        kr = holidays.country_holidays("KR", years=sorted(years))
        return {d for d in kr.keys()}
    except Exception:
        pass

    # 2순위: 하드코딩 백업
    import datetime as _dt
    holiday_dates = set()
    for y in years:
        for date_str in KOREAN_HOLIDAYS_FALLBACK.get(y, []):
            try:
                holiday_dates.add(
                    _dt.datetime.strptime(date_str, "%Y-%m-%d").date()
                )
            except Exception:
                continue
    return holiday_dates


def _is_workday(date_obj, holiday_set):
    """평일이면서 공휴일이 아니면 True."""
    if pd.isna(date_obj):
        return False
    # 월=0 ... 일=6, 토(5)/일(6) 제외
    if date_obj.weekday() >= 5:
        return False
    if hasattr(date_obj, "date"):
        return date_obj.date() not in holiday_set
    return date_obj not in holiday_set


def _calculate_measurement_hours(
    start_date, start_time, end_date, end_time, holiday_set,
):
    """업무시간(08:30~17:30, 공휴일/주말 제외) 안에서 시작~종료 사이 시간(시간 단위) 계산."""
    sd = _parse_date_value(start_date)
    ed = _parse_date_value(end_date)
    st_time = _parse_time_value(start_time)
    et_time = _parse_time_value(end_time)

    if pd.isna(sd) or pd.isna(ed) or st_time is None or et_time is None:
        return ""

    work_start = pd.Timestamp.combine(
        sd.date(), pd.Timestamp(0).replace(
            hour=WORK_START_HOUR, minute=WORK_START_MINUTE,
        ).time(),
    )
    work_end = pd.Timestamp.combine(
        sd.date(), pd.Timestamp(0).replace(
            hour=WORK_END_HOUR, minute=WORK_END_MINUTE,
        ).time(),
    )

    start_dt = pd.Timestamp.combine(sd.date(), st_time)
    end_dt = pd.Timestamp.combine(ed.date(), et_time)
    if end_dt < start_dt:
        return 0.0

    total_seconds = 0.0
    current_day = sd.normalize()
    last_day = ed.normalize()
    # 안전장치 (최대 365일까지만)
    safety_counter = 0
    while current_day <= last_day and safety_counter < 366:
        safety_counter += 1
        day_start = pd.Timestamp.combine(
            current_day.date(),
            pd.Timestamp(0).replace(hour=WORK_START_HOUR, minute=WORK_START_MINUTE).time(),
        )
        day_end = pd.Timestamp.combine(
            current_day.date(),
            pd.Timestamp(0).replace(hour=WORK_END_HOUR, minute=WORK_END_MINUTE).time(),
        )

        # 영업일이 아니면 0
        if _is_workday(current_day, holiday_set):
            # 해당 일의 유효 구간: max(start, 08:30) ~ min(end, 17:30)
            segment_start = max(start_dt, day_start)
            segment_end = min(end_dt, day_end)
            if segment_end > segment_start:
                total_seconds += (segment_end - segment_start).total_seconds()

        current_day = current_day + pd.Timedelta(days=1)

    return round(total_seconds / 3600.0, 2)


def _build_segment_to_product_id_lookups_local(detail_df):
    """현재 페이지 모듈 내에서 사용하기 위해 preprocess 모듈 함수를 호출.

    detail_df 가 비어있으면 빈 dict 반환.
    """
    if detail_df is None or detail_df.empty:
        return {}, {}
    try:
        from cost_summary_preprocess import _build_segment_to_product_id_lookups
        return _build_segment_to_product_id_lookups(detail_df)
    except Exception:
        return {}, {}


def _enrich_with_sales_type_and_product_id(
    combined_df, product_id_df,
):
    """통합 DataFrame 에 정비매출/위탁매출/사내매출/매출구분/구분자/상품ID 컬럼 추가.

    재료비 시트와 동일 로직:
        - product_id_df 에서 (매출구분, 신번호) 또는 (매출구분, 구번호) 별 카운트로
          각 매출구분 컬럼 채움
        - 매출구분 우선순위: 정비매출 → 위탁매출 → 사내매출
        - 구분자 = '{매출구분}_{차량번호}'
        - 상품ID = (매출구분, 신번호) 또는 (매출구분, 구번호) 로 product_id_df 매칭
    """
    if combined_df.empty:
        for col in ["정비매출", "위탁매출", "사내매출", "매출구분", "구분자", "상품ID"]:
            combined_df[col] = "" if col in ("매출구분", "구분자", "상품ID") else 0
        return combined_df

    # 매출구분별 차량번호 카운트 lookup
    sales_count_lookup = {
        "정비매출": {"신번호": {}, "구번호": {}},
        "위탁매출": {"신번호": {}, "구번호": {}},
        "사내매출": {"신번호": {}, "구번호": {}},
    }
    if product_id_df is not None and not product_id_df.empty:
        if all(c in product_id_df.columns for c in ["매출구분", "신번호", "구번호"]):
            detail = product_id_df.copy()
            detail["_매출구분"] = detail["매출구분"].astype(str).str.strip()
            detail["_신번호"] = detail["신번호"].astype(str).str.strip().apply(
                lambda v: re.sub(r"\s+", "", v) if v else ""
            )
            detail["_구번호"] = detail["구번호"].astype(str).str.strip().apply(
                lambda v: re.sub(r"\s+", "", v) if v else ""
            )
            for sales_type in sales_count_lookup.keys():
                rows = detail[detail["_매출구분"].eq(sales_type)]
                sales_count_lookup[sales_type]["신번호"] = (
                    rows.loc[rows["_신번호"].ne(""), "_신번호"].value_counts().to_dict()
                )
                sales_count_lookup[sales_type]["구번호"] = (
                    rows.loc[rows["_구번호"].ne(""), "_구번호"].value_counts().to_dict()
                )

    new_no_lookup, old_no_lookup = _build_segment_to_product_id_lookups_local(product_id_df)

    # 각 행마다 매출구분별 카운트 계산
    car_keys = combined_df["차량번호"].apply(
        lambda v: re.sub(r"\s+", "", str(v).strip()) if pd.notna(v) and str(v).strip() else ""
    )

    for sales_type in ["정비매출", "위탁매출", "사내매출"]:
        combined_df[sales_type] = [
            sales_count_lookup[sales_type]["신번호"].get(key, 0)
            + sales_count_lookup[sales_type]["구번호"].get(key, 0)
            for key in car_keys
        ]

    # 매출구분 우선순위: 정비 → 위탁 → 사내
    combined_df["매출구분"] = np.select(
        [
            combined_df["정비매출"].gt(0),
            combined_df["위탁매출"].gt(0),
            combined_df["사내매출"].gt(0),
        ],
        ["정비매출", "위탁매출", "사내매출"],
        default="",
    )

    # 구분자 = 매출구분_차량번호
    sales_type_series = combined_df["매출구분"].astype(str).str.strip()
    vehicle_numbers = combined_df["차량번호"].apply(
        lambda v: "" if pd.isna(v) else str(v).strip()
    )
    combined_df["구분자"] = np.where(
        sales_type_series.ne("") & vehicle_numbers.ne(""),
        sales_type_series + "_" + vehicle_numbers,
        "",
    )

    # 상품ID = (매출구분, 신번호) 또는 (매출구분, 구번호) lookup
    product_ids = []
    cache = {}
    for segment_key in combined_df["구분자"]:
        key = str(segment_key).strip() if pd.notna(segment_key) else ""
        if not key:
            product_ids.append("")
            continue
        if key not in cache:
            pid = new_no_lookup.get(key, "")
            if not pid:
                pid = old_no_lookup.get(key, "")
            cache[key] = pid
        product_ids.append(cache[key])
    combined_df["상품ID"] = product_ids

    return combined_df


def build_combined_cost_driver_df(
    cost_driver_dfs,
    settlement_year=None,
    settlement_month=None,
    product_id_df=None,
):
    """원가동인 dict (AQI실적/TS/RTLS) 를 단일 DataFrame 으로 통합.

    반환 컬럼: [공정, 차량번호, 모델명, 최초측정일, 최초측정시간,
              최종측정일, 최종측정시간, 담당자, 구분, 측정시간(H),
              발생연도, 발생월,
              정비매출, 위탁매출, 사내매출, 매출구분, 구분자, 상품ID]
    """
    extra_columns = [
        "구분", "측정시간(H)", "발생연도", "발생월",
        "정비매출", "위탁매출", "사내매출", "매출구분", "구분자", "상품ID",
    ]
    if not cost_driver_dfs:
        return pd.DataFrame(columns=COMBINED_DRIVER_COLUMNS + extra_columns)

    normalizers = {
        "RTLS": _normalize_rtls_sheet,
        "TS": _normalize_ts_sheet,
        "AQI실적": _normalize_aqi_sheet,
    }

    frames = []
    for keyword, normalizer in normalizers.items():
        sheet_map = cost_driver_dfs.get(keyword)
        if not sheet_map:
            continue
        for sheet_name, df in sheet_map.items():
            if df is None or df.empty:
                continue
            try:
                normalized = normalizer(df, sheet_name)
            except Exception as exc:
                st.warning(f"[{keyword} / {sheet_name}] 통합 중 오류: {exc}")
                continue
            if not normalized.empty:
                frames.append(normalized)

    if not frames:
        return pd.DataFrame(columns=COMBINED_DRIVER_COLUMNS + extra_columns)

    combined = pd.concat(frames, ignore_index=True)
    combined = combined[COMBINED_DRIVER_COLUMNS].copy()

    # 구분 컬럼: TU/PL/PA → 정비/판금/도장 (그 외는 원값)
    combined["구분"] = combined["공정"].apply(_normalize_process_to_category)

    # 측정시간(H) 계산
    holiday_set = _build_korean_holiday_checker(year_hint=settlement_year)

    def _row_hours(row):
        if str(row["구분"]).strip() == "TS":
            return 0.5
        return _calculate_measurement_hours(
            row["최초측정일"], row["최초측정시간"],
            row["최종측정일"], row["최종측정시간"],
            holiday_set,
        )
    combined["측정시간(H)"] = combined.apply(_row_hours, axis=1)

    # 발생연도/발생월 = 결산연도/결산월
    combined["발생연도"] = int(settlement_year) if settlement_year is not None else pd.NA
    combined["발생월"] = int(settlement_month) if settlement_month is not None else pd.NA

    # 매출구분/구분자/상품ID 채우기 (재료비와 동일 로직)
    combined = _enrich_with_sales_type_and_product_id(combined, product_id_df)

    # 최종 컬럼 순서
    return combined[COMBINED_DRIVER_COLUMNS + extra_columns]


def render_combined_cost_driver(
    cost_driver_dfs, settlement_year=None, settlement_month=None, product_id_df=None,
):
    """통합 원가동인 DataFrame 표시."""
    combined_df = build_combined_cost_driver_df(
        cost_driver_dfs, settlement_year, settlement_month, product_id_df,
    )
    if combined_df.empty:
        return combined_df

    with st.expander("원가동인 통합 (AQI실적 / TS / RTLS)"):
        st.write(f"통합 건수: {len(combined_df):,}건")

        # 공휴일 처리 상태 표시 (측정시간 계산 디버깅용)
        if settlement_year is not None:
            holiday_set = _build_korean_holiday_checker(settlement_year)
            year_holidays = sorted(
                d for d in holiday_set if d.year == int(settlement_year)
            )
            try:
                import holidays as _h  # noqa: F401
                holiday_source = "holidays 패키지"
            except Exception:
                holiday_source = "내장 백업 데이터 (KOREAN_HOLIDAYS_FALLBACK)"
            st.caption(
                f"공휴일 처리: {holiday_source} | "
                f"{int(settlement_year)}년 공휴일 {len(year_holidays)}개"
            )

        st.download_button(
            "통합 원가동인 다운로드",
            data=dataframe_to_excel_bytes(combined_df, sheet_name="원가동인통합"),
            file_name="cost_driver_combined.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="cost_driver_combined_download",
        )
        st.dataframe(dataframe_for_display(combined_df), use_container_width=True)

    return combined_df


# ============================================================
# 7. 검증시트 업로드
# ============================================================

def render_verification_sheet_upload():
    st.markdown("##### 3-2 기간별제조원가보고서")

    uploaded_file = st.file_uploader(
        "기간별제조원가보고서 업로드",
        type=["xlsx", "xls"], accept_multiple_files=False, key="verification_sheet_file",
    )

    if uploaded_file is None:
        return None

    try:
        # header=0 → 1번째 행이 컬럼명
        sheets = pd.read_excel(uploaded_file, sheet_name=None, header=0)
    except Exception as exc:
        st.error(f"{uploaded_file.name} 처리 중 오류: {exc}")
        return None

    # 시트가 여러 개일 수 있으니 dict 형태로 반환
    cleaned_sheets = {}
    for sheet_name, df in sheets.items():
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
        cleaned_sheets[sheet_name] = df

    if cleaned_sheets:
        with st.expander("검증시트 데이터 확인"):
            render_dataframe_tabs(cleaned_sheets)

    return cleaned_sheets


# ============================================================
# 8. 최종 원가 렌더링
# ============================================================

def render_final_cost(
    product_id_df, purchase_cost_sheet_dfs, dfs, settlement_month,
    manufacturing_cost_sheet_dfs=None,
    verification_sheets=None,
    settlement_year=None,
    cost_driver_dfs=None,
    combined_cost_driver_df=None,
):
    st.subheader("4 차량별 원가현황")

    final_cost_df, _cached_diag = _cached_build_final_cost_df(
        product_id_df,
        purchase_cost_sheet_dfs,
        dfs.get("기초재고_전체"),
        settlement_month,
        manufacturing_cost_sheet_dfs,
        verification_sheets,
        settlement_year,
        cost_driver_dfs,
        combined_cost_driver_df,
        dfs.get("위탁수불부_전체"),
    )

    if final_cost_df.empty:
        st.info("1번 기초 DB 데이터를 업로드하면 최종 원가 초안이 생성됩니다.")
        return final_cost_df

    if not purchase_cost_sheet_dfs:
        st.info("2-1 매입원가의 상품원장 데이터를 업로드하면 금액 컬럼이 채워집니다.")

    # 제조경비 배부 내역 (분자 / 분모 / 단가)
    # 캐시된 진단 우선 → attrs → 모듈 백업 순
    expense_diagnostics = _cached_diag
    if not expense_diagnostics:
        expense_diagnostics = final_cost_df.attrs.get("제조경비_배부내역")
    if not expense_diagnostics:
        expense_diagnostics = get_last_manufacturing_expense_diagnostics()
    if expense_diagnostics:
        with st.expander("🔍 제조경비 배부 내역 (배부총액 ÷ 가중치합 = 단가)"):
            diag_df = pd.DataFrame(expense_diagnostics)
            display_diag = diag_df.copy()

            # 컬럼 순서: 컬럼 / 배부총액(분자) / 실제배부값합 / 가중치합(분모) / 단가 / 비고
            preferred_order = [
                "컬럼", "배부총액(분자)", "실제배부값합",
                "가중치합(분모)", "단가", "비고",
            ]
            ordered = [c for c in preferred_order if c in display_diag.columns]
            ordered += [c for c in display_diag.columns if c not in ordered]
            display_diag = display_diag[ordered]

            # 금액 컬럼(분자/실제배부값): 정수, 천단위 콤마
            for col in ["배부총액(분자)", "실제배부값합"]:
                if col in display_diag.columns:
                    display_diag[col] = display_diag[col].apply(
                        lambda v: f"{round(v):,}" if isinstance(v, (int, float)) else v
                    )
            # 가중치합(분모): 실제 계산에 쓰는 소수값 그대로 표시 (라운드하면 단가 검산이 안 맞음)
            if "가중치합(분모)" in display_diag.columns:
                display_diag["가중치합(분모)"] = display_diag["가중치합(분모)"].apply(
                    lambda v: (f"{v:,.4f}".rstrip("0").rstrip(".") if isinstance(v, (int, float)) else v)
                )
            # 단가: 소수점 유지 (배부 비율이라 정밀도 필요)
            if "단가" in display_diag.columns:
                display_diag["단가"] = display_diag["단가"].apply(
                    lambda v: f"{v:,.4f}".rstrip("0").rstrip(".") if isinstance(v, (int, float)) else v
                )
            st.dataframe(display_diag, use_container_width=True)

    st.download_button(
        "차량별 원가 다운로드",
        data=dataframe_to_excel_bytes(final_cost_df, sheet_name="차량별원가"),
        file_name="final_cost_draft.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    st.write(f"건수: {len(final_cost_df):,}건")
    st.dataframe(dataframe_for_display(final_cost_df), use_container_width=True)

    return final_cost_df


# ============================================================
# 9. 페이지 엔트리
# ============================================================

st.set_page_config(page_title="손익분석", layout="wide")
st.title("Cost Summary")

# 최종 마스터 저장 경로 (코드와 같은 폴더, 서버 공용)
import os as _os
import glob as _glob


def _master_dir():
    """마스터 파일 저장 폴더 반환.

    코드가 'pages/' 안에 있으면 그 부모 폴더(앱 루트)를 사용.
    그 외에는 코드 같은 폴더를 사용.
    """
    here = _os.path.dirname(_os.path.abspath(__file__))
    if _os.path.basename(here).lower() == "pages":
        return _os.path.dirname(here)
    return here


MASTER_FILE_PATH = _os.path.join(
    _master_dir(), f"cost_summary_{datetime.now().strftime('%Y%m%d')}.xlsx"
)
MASTER_META_PATH = _os.path.join(
    _master_dir(), "final_cost_master_meta.txt"
)

# 누적 키: 이 조합이 같은 행은 새 데이터로 교체, 다른 행은 추가
_MASTER_ACCUMULATION_KEYS = ["상품ID", "매출구분", "회계연도", "회계월"]


def _find_latest_cost_summary_path():
    """마스터 폴더에서 가장 최근 cost_summary_*.xlsx 경로 반환 (없으면 None)."""
    here = _master_dir()
    matched = [
        p for p in _glob.glob(_os.path.join(here, "cost_summary_*.xlsx"))
        if _os.path.exists(p) and _os.path.getsize(p) > 0
    ]
    if not matched:
        return None
    matched.sort(key=lambda p: _os.path.basename(p), reverse=True)
    return matched[0]


def _read_existing_master_df():
    """최신 cost_summary_*.xlsx 를 읽어 반환 (없으면 None)."""
    path = _find_latest_cost_summary_path()
    if path is None:
        return None
    try:
        return pd.read_excel(path, sheet_name="최종원가마스터")
    except Exception:
        try:
            return pd.read_excel(path)
        except Exception:
            return None


def _accumulate_master_data(existing_df, new_df):
    """기존 마스터에 새 데이터를 누적.

    키 = (상품ID, 매출구분, 회계연도, 회계월). 같은 키 행은 새 데이터로 교체.
    없는 키 행은 추가. 컬럼은 합집합.
    반환: (누적 df, 교체된 행 수)
    """
    if existing_df is None or existing_df.empty:
        return new_df.copy(), 0

    for c in _MASTER_ACCUMULATION_KEYS:
        if c not in existing_df.columns or c not in new_df.columns:
            return new_df.copy(), 0

    def _normalize_keys(df):
        df = df.copy()
        for c in ["상품ID", "매출구분"]:
            df[c] = df[c].astype(str).str.strip()
        for c in ["회계연도", "회계월"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    existing_norm = _normalize_keys(existing_df)
    new_norm = _normalize_keys(new_df)

    new_keys = set(zip(
        new_norm["상품ID"], new_norm["매출구분"],
        new_norm["회계연도"], new_norm["회계월"],
    ))
    existing_keys = list(zip(
        existing_norm["상품ID"], existing_norm["매출구분"],
        existing_norm["회계연도"], existing_norm["회계월"],
    ))
    keep_mask = pd.Series(
        [k not in new_keys for k in existing_keys], index=existing_norm.index,
    )
    kept = existing_norm[keep_mask]
    replaced_count = int((~keep_mask).sum())

    result = pd.concat([kept, new_norm], ignore_index=True, sort=False)
    return result, replaced_count


def save_final_master(final_cost_df):
    """최종원가 결과를 누적해 서버 엑셀 파일로 저장 (공용).

    1) 최신 cost_summary_*.xlsx 를 읽어 기존 마스터로 사용 (없으면 빈 시작)
    2) 같은 (상품ID, 매출구분, 회계연도, 회계월) 행 교체 + 나머지 추가
    3) 컬럼은 합집합으로 처리
    4) 오늘 날짜 파일(cost_summary_YYYYMMDD.xlsx) 로 저장

    반환: (저장 시각, 누적 후 총 행 수, 이번 빌드 행 수, 교체된 행 수)
    """
    saved_at = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

    existing = _read_existing_master_df()
    merged, replaced = _accumulate_master_data(existing, final_cost_df)

    today_path = _os.path.join(
        _master_dir(),
        f"cost_summary_{datetime.now().strftime('%Y%m%d')}.xlsx",
    )
    with pd.ExcelWriter(today_path, engine="openpyxl") as writer:
        merged.to_excel(writer, index=False, sheet_name="최종원가마스터")
    with open(MASTER_META_PATH, "w", encoding="utf-8") as f:
        f.write(saved_at)
    return saved_at, len(merged), len(final_cost_df), replaced


@st.cache_data(show_spinner=False)
def _read_master_excel_cached(path, mtime):
    """엑셀 마스터 읽기 (파일 수정시각 mtime 을 캐시 키로 사용)."""
    try:
        return pd.read_excel(path, sheet_name="최종원가마스터")
    except Exception:
        return pd.read_excel(path)


def load_final_master():
    """저장된 최종원가 결과(엑셀) 불러오기. (df, 저장시각) 또는 (None, None).

    최신 cost_summary_*.xlsx 를 찾아 읽는다.
    """
    path = _find_latest_cost_summary_path()
    if path is None:
        return None, None
    try:
        mtime = _os.path.getmtime(path)
        df = _read_master_excel_cached(path, mtime)
    except Exception:
        return None, None
    saved_at = None
    if _os.path.exists(MASTER_META_PATH):
        try:
            with open(MASTER_META_PATH, "r", encoding="utf-8") as f:
                saved_at = f.read().strip()
        except Exception:
            saved_at = None
    return df, saved_at


def delete_final_master():
    """모든 cost_summary_*.xlsx 파일과 메타 삭제. 삭제 성공 여부 반환."""
    deleted = False
    here = _master_dir()
    for path in _glob.glob(_os.path.join(here, "cost_summary_*.xlsx")):
        try:
            _os.remove(path)
            deleted = True
        except Exception:
            pass
    if _os.path.exists(MASTER_META_PATH):
        try:
            _os.remove(MASTER_META_PATH)
            deleted = True
        except Exception:
            pass
    return deleted


# ----- 회계처리 표 -----

# 월 + 누계 컬럼 (각 항목마다 대수/금액 2개씩)
_ACCOUNTING_MONTH_LABELS = [f"{m}월" for m in range(1, 13)] + ["누계"]

# 위탁매출 거래처 목록 (이 순서대로 표시, '기타'는 나머지)
_CONSIGNMENT_PARTNERS = [
    "하나캐피탈", "우리금융캐피탈", "NH농협캐피탈", "삼성카드",
    "현대캐피탈(직)", "MG캐피탈", "레드캡투어", "롯데렌탈", "현대캐피탈(플랫폼)",
]
# 위탁매출 거래처를 담은 컬럼 (필요시 변경)
_CONSIGNMENT_PARTNER_COLUMN = "분류3"


def _num(series):
    return pd.to_numeric(series, errors="coerce").fillna(0)


def _compute_accounting_tables(master_df):
    """최종원가마스터에서 회계처리 표 3개의 값을 월별로 계산.

    반환: {"auto": df, "sales": df, "consign": df} (각 멀티헤더 표)
    """
    df = master_df.copy()
    df.columns = [str(c) for c in df.columns]

    month_series = (
        pd.to_numeric(df["회계월"], errors="coerce")
        if "회계월" in df.columns
        else pd.Series([pd.NA] * len(df), index=df.index)
    )
    sales_series = (
        df["매출구분"].astype(str).str.strip()
        if "매출구분" in df.columns
        else pd.Series([""] * len(df), index=df.index)
    )

    def col(name):
        return _num(df[name]) if name in df.columns else pd.Series([0.0] * len(df), index=df.index)

    def partner_series():
        if _CONSIGNMENT_PARTNER_COLUMN in df.columns:
            return df[_CONSIGNMENT_PARTNER_COLUMN].astype(str).str.strip()
        return pd.Series([""] * len(df), index=df.index)

    auto_rows = [
        ("기초재고", 0), ("당기입고", 0), ("정상입고", 1), ("타계정입고", 1), ("제조원가", 1),
        ("당기출고", 0), ("정상출고", 1), ("자산출고", 1), ("기타출고", 1), ("기말재고", 0),
    ]
    auto_table = _build_accounting_table(auto_rows)

    sales_rows = [
        ("보험매출", 0), ("정비매출", 0), ("검사매출", 0), ("위탁매출", 0),
    ] + [(p, 1) for p in _CONSIGNMENT_PARTNERS] + [("기타", 1)]
    sales_table = _build_accounting_table(sales_rows)

    consign_rows = [
        ("기초재고", 0), ("제조원가", 1),
        ("당기입고", 0), ("제조원가", 1),
        ("당기출고", 0), ("매출원가", 1), ("위탁판매", 2), ("위탁매입", 2), ("위탁취소", 2),
        ("기말재고", 0),
    ]
    consign_table = _build_accounting_table(consign_rows)

    for month_label in _ACCOUNTING_MONTH_LABELS:
        if month_label == "누계":
            month_mask = pd.Series([True] * len(df), index=df.index)
        else:
            m = int(month_label.replace("월", ""))
            month_mask = month_series.eq(m)

        in_house = month_mask & sales_series.eq("사내매출")
        consign_m = month_mask & sales_series.eq("위탁매출")

        def s(mask, name):
            return float(col(name)[mask].sum())

        # ① 자동차 수불 (사내매출)
        base_q = s(in_house, "기초_수량"); base_a = s(in_house, "기초_금액")
        normal_in_q = s(in_house, "정상입고_수량"); normal_in_a = s(in_house, "정상입고_금액")
        transfer_in_q = s(in_house, "타처입고_수량"); transfer_in_a = s(in_house, "타처입고_금액")
        mfg_a = s(month_mask, "제조원가_당월")
        in_q = normal_in_q + transfer_in_q
        in_a = normal_in_a + transfer_in_a + mfg_a
        normal_out_q = s(in_house, "정상출고_수량"); normal_out_a = s(in_house, "정상출고_금액")
        asset_out_q = s(in_house, "자산출고_수량"); asset_out_a = s(in_house, "자산출고_금액")
        etc_out_q = s(in_house, "기타출고_수량"); etc_out_a = s(in_house, "기타출고_금액")
        out_q = normal_out_q + asset_out_q + etc_out_q
        out_a = normal_out_a + asset_out_a + etc_out_a
        end_q = base_q + in_q - out_q
        end_a = base_a + in_a - out_a

        auto_values = {
            "기초재고": (base_q, base_a),
            "당기입고": (in_q, in_a),
            "정상입고": (normal_in_q, normal_in_a),
            "타계정입고": (transfer_in_q, transfer_in_a),
            "제조원가": (0, mfg_a),
            "당기출고": (out_q, out_a),
            "정상출고": (normal_out_q, normal_out_a),
            "자산출고": (asset_out_q, asset_out_a),
            "기타출고": (etc_out_q, etc_out_a),
            "기말재고": (end_q, end_a),
        }
        col_q = auto_table.columns.get_loc((month_label, "대수"))
        col_a = auto_table.columns.get_loc((month_label, "금액"))
        for row_pos, (name, level) in enumerate(auto_rows):
            q, a = auto_values[name]
            auto_table.iloc[row_pos, col_q] = q
            auto_table.iloc[row_pos, col_a] = a

        # ② 매출구분별
        def sales_count_amount(mask):
            return int(mask.sum()), float(col("제조원가")[mask].sum())

        ins_q, ins_a = sales_count_amount(month_mask & sales_series.eq("보험매출"))
        mnt_q, mnt_a = sales_count_amount(month_mask & sales_series.eq("정비매출"))
        insp_q, insp_a = sales_count_amount(month_mask & sales_series.eq("검사매출"))
        cons_q, cons_a = sales_count_amount(consign_m)

        sales_values = {
            "보험매출": (ins_q, ins_a),
            "정비매출": (mnt_q, mnt_a),
            "검사매출": (insp_q, insp_a),
            "위탁매출": (cons_q, cons_a),
        }
        partners = partner_series()
        partner_sum_q = 0
        partner_sum_a = 0.0
        for p in _CONSIGNMENT_PARTNERS:
            pq, pa = sales_count_amount(consign_m & partners.eq(p))
            sales_values[p] = (pq, pa)
            partner_sum_q += pq
            partner_sum_a += pa
        sales_values["기타"] = (cons_q - partner_sum_q, cons_a - partner_sum_a)

        col_q = sales_table.columns.get_loc((month_label, "대수"))
        col_a = sales_table.columns.get_loc((month_label, "금액"))
        for row_pos, (name, level) in enumerate(sales_rows):
            q, a = sales_values[name]
            sales_table.iloc[row_pos, col_q] = q
            sales_table.iloc[row_pos, col_a] = a

        # ③ 위탁 수불 (위탁매출)
        c_base_q = s(consign_m, "기초_수량")
        c_base_a = float(col("제조원가")[consign_m & col("기초_수량").gt(0)].sum())
        c_in_q = s(consign_m, "정상입고_수량")
        c_in_a = float(col("제조원가")[consign_m & col("정상입고_수량").gt(0)].sum())
        c_out_q = s(consign_m, "정상출고_수량")
        c_out_a = s(consign_m, "정상출고_금액")
        status = (
            df["위탁출고구분"].astype(str).str.strip()
            if "위탁출고구분" in df.columns
            else pd.Series([""] * len(df), index=df.index)
        )
        def consign_status(value):
            mask = consign_m & status.eq(value)
            return s(mask, "정상출고_수량"), s(mask, "정상출고_금액")
        sale_q, sale_a = consign_status("위탁판매")
        buy_q, buy_a = consign_status("위탁매입")
        cancel_q, cancel_a = consign_status("위탁취소")
        c_end_q = c_base_q + c_in_q - c_out_q
        c_end_a = c_base_a + c_in_a - c_out_a

        consign_values_ordered = [
            (c_base_q, c_base_a),
            (c_base_q, c_base_a),
            (c_in_q, c_in_a),
            (c_in_q, c_in_a),
            (c_out_q, c_out_a),
            (c_out_q, c_out_a),
            (sale_q, sale_a),
            (buy_q, buy_a),
            (cancel_q, cancel_a),
            (c_end_q, c_end_a),
        ]
        col_q = consign_table.columns.get_loc((month_label, "대수"))
        col_a = consign_table.columns.get_loc((month_label, "금액"))
        for row_pos, value in enumerate(consign_values_ordered):
            q, a = value
            consign_table.iloc[row_pos, col_q] = q
            consign_table.iloc[row_pos, col_a] = a

    return {"auto": auto_table, "sales": sales_table, "consign": consign_table}


def _build_accounting_table(row_specs):
    """회계처리용 빈 표 생성.

    row_specs: [(행이름, 들여쓰기레벨), ...]
    반환: 멀티헤더(월/누계 × 대수/금액) DataFrame, 값은 0.
    중복 라벨은 zero-width space 로 고유화 (Styler 가 비고유 인덱스를 거부하므로).
    """
    columns = pd.MultiIndex.from_product(
        [_ACCOUNTING_MONTH_LABELS, ["대수", "금액"]]
    )
    index_labels = []
    seen = {}
    for name, level in row_specs:
        label = ("\u00a0" * (level * 4)) + name
        # 중복이면 보이지 않는 zero-width space(\u200b) 를 개수만큼 붙여 고유화
        count = seen.get(label, 0)
        seen[label] = count + 1
        if count > 0:
            label = label + ("\u200b" * count)
        index_labels.append(label)
    data = [[0] * len(columns) for _ in row_specs]
    df = pd.DataFrame(data, index=index_labels, columns=columns)
    return df


def _render_accounting_table(title, table, highlight_rows):
    """회계처리 표 하나 렌더링 (강조행 색상+볼드, 누계 컬럼 강조)."""
    st.markdown(f"**{title}**")

    highlight_positions = set(highlight_rows)
    n_rows = len(table)
    n_cols = len(table.columns)

    # 위치 기반 스타일 매트릭스 (중복 인덱스 라벨 대응)
    style_df = pd.DataFrame("", index=table.index, columns=table.columns)
    for r in range(n_rows):
        row_style = ""
        if r in highlight_positions:
            row_style = "background-color: #fce4d6; font-weight: bold;"
        for c in range(n_cols):
            col_tuple = table.columns[c]
            border = ""
            if isinstance(col_tuple, tuple) and col_tuple[0] == "누계":
                border = "border-left: 2px solid #c00000;"
            style_df.iloc[r, c] = row_style + border

    styler = table.style.format("{:,.0f}").apply(lambda _: style_df, axis=None)
    st.dataframe(styler, use_container_width=True)


def render_accounting_section(master_df=None):
    """회계처리 섹션: 상품(자동차) 수불 / 매출구분별 / 상품(위탁) 수불."""
    st.subheader("회계처리")

    if master_df is None or master_df.empty:
        st.info("최종 원가 마스터가 저장되면 회계처리 표가 채워집니다.")
        return

    tables = _compute_accounting_tables(master_df)
    # 강조행: 소계/합계 (행 위치 기준)
    # ① 자동차: 기초재고0, 당기입고1, 당기출고5, 기말재고9
    _render_accounting_table("상품(자동차) 수불", tables["auto"], [0, 1, 5, 9])
    # ② 위탁: 기초재고0, 당기입고2, 당기출고4, 기말재고9
    _render_accounting_table("상품(위탁) 수불", tables["consign"], [0, 2, 4, 9])
    # # ③ 매출구분별: 위탁매출(3)만 강조
    # _render_accounting_table("매출구분별", tables["sales"], [3])     # 매출구분별 표


tab1, tab2 = st.tabs(["VIEW", "UPLOAD"])

with tab1:
    master_df, master_saved_at = load_final_master()

    # 회계처리 (먼저)
    render_accounting_section(master_df)

    st.divider()

    # 차량별 원가
    st.subheader("차량별 원가")
    if master_df is None:
        st.info("저장된 최종 원가 마스터가 없습니다. UPLOAD 탭에서 생성 후 '최종 마스터 저장'을 눌러주세요.")
    else:
        if master_saved_at:
            st.caption(f"마지막 저장: {master_saved_at}")
        st.write(f"건수: {len(master_df):,}건")
        st.download_button(
            "최종 원가 마스터 다운로드",
            data=dataframe_to_excel_bytes(master_df, sheet_name="최종원가마스터"),
            file_name=f"cost_summary_{datetime.now().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        st.dataframe(dataframe_for_display(master_df), use_container_width=True)

        # 데이터 초기화 (저장본 삭제)
        with st.expander("⚠️ 데이터 초기화"):
            st.caption("저장된 원가 데이터를 삭제합니다. 이 작업은 되돌릴 수 없습니다.")
            confirm_reset = st.checkbox(
                "삭제를 확인합니다", key="confirm_master_reset"
            )
            if st.button(
                "데이터 초기화",
                key="reset_master",
                type="secondary",
                disabled=not confirm_reset,
            ):
                if delete_final_master():
                    st.success("원가 데이터가 초기화되었습니다.")
                    st.rerun()
                else:
                    st.warning("삭제할 데이터가 없습니다.")

with tab2:
    settlement_year, settlement_month = render_settlement_selector()
    dfs, product_id_df = render_base_upload(settlement_year, settlement_month)

    st.divider()
    st.subheader("2 원가금액")
    purchase_cost_sheet_dfs = render_purchase_cost_upload(
        product_id_df, dfs, settlement_year, settlement_month,
    )

    st.divider()
    manufacturing_cost_sheet_dfs = render_manufacturing_cost_upload(
        product_id_df, settlement_year, settlement_month,
    )

    st.divider()
    cost_driver_dfs = render_cost_driver_upload(settlement_year, settlement_month)
    combined_cost_driver_df = render_combined_cost_driver(
        cost_driver_dfs, settlement_year, settlement_month, product_id_df,
    )

    st.divider()
    verification_sheets = render_verification_sheet_upload()

    st.divider()
    final_cost_df = render_final_cost(
        product_id_df, purchase_cost_sheet_dfs, dfs, settlement_month,
        manufacturing_cost_sheet_dfs=manufacturing_cost_sheet_dfs,
        verification_sheets=verification_sheets,
        settlement_year=settlement_year,
        cost_driver_dfs=cost_driver_dfs,
        combined_cost_driver_df=combined_cost_driver_df,
    )

    # 최종 마스터 저장 (서버 공용 — VIEW 탭에서 누구나 조회 가능)
    st.divider()
    if final_cost_df is not None and not final_cost_df.empty:
        if st.button("💾 최종 마스터 저장", key="save_final_master", type="primary"):
            saved_at, total_rows, new_rows, replaced_rows = save_final_master(final_cost_df)
            added_rows = new_rows - replaced_rows
            st.success(
                f"최종 마스터가 저장되었습니다. ({saved_at})\n\n"
                f"이번 빌드: {new_rows:,}건 (신규 추가 {added_rows:,}건, 기존 교체 {replaced_rows:,}건)\n"
                f"누적 마스터 총 {total_rows:,}건. VIEW 탭에서 확인하세요."
            )
    else:
        st.caption("최종 원가 데이터가 생성되면 '최종 마스터 저장' 버튼이 활성화됩니다.")