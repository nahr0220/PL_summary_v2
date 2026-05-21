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
from cost_summary_builder import build_final_cost_df


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
            st.dataframe(dataframe_for_display(current_df), width='stretch')


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
    st.header("1️⃣ 기초 DB")

    uploaded_files = st.file_uploader(
        "파일 업로드하세요.", type=["xlsx"], accept_multiple_files=True,
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

        st.divider()
        st.subheader("🧾 상품ID 모음")
        product_id_df = collect_product_ids(dfs, settlement_year, settlement_month)

        if not product_id_df.empty:
            st.write(f"통합 데이터 건수: {len(product_id_df):,}건")
            with st.expander("구분 포함 상세 보기"):
                st.download_button(
                    "엑셀 다운로드",
                    data=dataframe_to_excel_bytes(product_id_df, sheet_name="구분포함상세"),
                    file_name="product_id_detail.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                st.dataframe(dataframe_for_display(product_id_df), width='stretch')
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
    st.subheader("2-1. 매입원가")

    uploaded_cost_files = st.file_uploader(
        "매입원가 파일을 업로드하세요.",
        type=["xlsx", "xls"], accept_multiple_files=True, key="cost_files",
    )

    if not uploaded_cost_files:
        return {}
    if len(uploaded_cost_files) > 3:
        st.warning("원가 파일은 3개까지 업로드하는 기준으로 처리합니다.")

    cost_sheet_dfs = preprocess_purchase_cost_files(
        uploaded_cost_files, product_id_df, dfs, settlement_year, settlement_month,
    )
    render_sheet_workbook(
        cost_sheet_dfs,
        "매입원가 전처리 파일 다운로드",
        "purchase_cost_preprocessed.xlsx",
        "매입원가 파일에서 표시할 데이터가 없습니다.",
    )
    return cost_sheet_dfs


# ============================================================
# 5. 제조원가 업로드
# ============================================================

def render_manufacturing_cost_upload(product_id_df, settlement_year, settlement_month):
    st.subheader("2-2. 제조원가")

    uploaded_files = st.file_uploader(
        "제조원가 파일을 업로드하세요.",
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
        "제조원가 전처리 파일 다운로드",
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
    st.header("3️⃣ 원가동인")

    uploaded_files = st.file_uploader(
        "원가동인 파일을 업로드하세요. (AQI실적 / TS / RTLS / rtc / sm)",
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
                f"{file.name}: AQI실적, TS, RTLS, rtc, sm 중 하나가 파일명에 포함되어야 합니다."
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


def _build_korean_holiday_checker(year_hint=None):
    """한국 공휴일 set (datetime.date 집합) 반환.

    1순위: holidays 패키지 사용
    2순위: KOREAN_HOLIDAYS_FALLBACK 하드코딩 데이터 사용
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
        st.dataframe(dataframe_for_display(combined_df), width="stretch")

    return combined_df


# ============================================================
# 7. 검증시트 업로드
# ============================================================

def render_verification_sheet_upload():
    st.header("4️⃣ 검증시트")

    uploaded_file = st.file_uploader(
        "검증시트 파일을 업로드하세요.",
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
    st.header("5️⃣ 최종 원가 생성")

    final_cost_df = build_final_cost_df(
        product_id_df,
        purchase_cost_sheet_dfs,
        dfs.get("기초재고_전체"),
        settlement_month,
        manufacturing_cost_sheet_dfs=manufacturing_cost_sheet_dfs,
        verification_sheets=verification_sheets,
        settlement_year=settlement_year,
        cost_driver_dfs=cost_driver_dfs,
        combined_cost_driver_df=combined_cost_driver_df,
    )

    if final_cost_df.empty:
        st.info("1번 기초 DB 데이터를 업로드하면 최종 원가 초안이 생성됩니다.")
        return final_cost_df

    if not purchase_cost_sheet_dfs:
        st.info("2-1 매입원가의 상품원장 데이터를 업로드하면 금액 컬럼이 채워집니다.")

    # 제조경비 배부 내역 (분자 / 분모 / 단가)
    expense_diagnostics = final_cost_df.attrs.get("제조경비_배부내역")
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

            # 금액 컬럼: 정수, 천단위 콤마
            for col in ["배부총액(분자)", "실제배부값합", "가중치합(분모)"]:
                if col in display_diag.columns:
                    display_diag[col] = display_diag[col].apply(
                        lambda v: f"{round(v):,}" if isinstance(v, (int, float)) else v
                    )
            # 단가: 소수점 유지 (배부 비율이라 정밀도 필요)
            if "단가" in display_diag.columns:
                display_diag["단가"] = display_diag["단가"].apply(
                    lambda v: f"{v:,.4f}".rstrip("0").rstrip(".") if isinstance(v, (int, float)) else v
                )
            st.dataframe(display_diag, width="stretch")

    st.download_button(
        "최종 원가 초안 다운로드",
        data=dataframe_to_excel_bytes(final_cost_df, sheet_name="최종원가초안"),
        file_name="final_cost_draft.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    st.write(f"건수: {len(final_cost_df):,}건")
    st.dataframe(dataframe_for_display(final_cost_df), width='stretch')

    return final_cost_df


# ============================================================
# 9. 페이지 엔트리
# ============================================================

st.set_page_config(page_title="손익분석", layout="wide")
st.title("Cost Summary")

tab1, tab2 = st.tabs(["VIEW", "UPLOAD"])

with tab1:
    st.write("준비 중")

with tab2:
    settlement_year, settlement_month = render_settlement_selector()
    dfs, product_id_df = render_base_upload(settlement_year, settlement_month)

    st.divider()
    st.header("2️⃣ 원가")
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
    render_final_cost(
        product_id_df, purchase_cost_sheet_dfs, dfs, settlement_month,
        manufacturing_cost_sheet_dfs=manufacturing_cost_sheet_dfs,
        verification_sheets=verification_sheets,
        settlement_year=settlement_year,
        cost_driver_dfs=cost_driver_dfs,
        combined_cost_driver_df=combined_cost_driver_df,
    )