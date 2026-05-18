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
                file_sheets = preprocess_direct_expense_file(file)
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
# 6. 최종 원가 렌더링
# ============================================================

def render_final_cost(product_id_df, purchase_cost_sheet_dfs, dfs, settlement_month):
    st.header("4️⃣ 최종 원가 생성")

    final_cost_df = build_final_cost_df(
        product_id_df,
        purchase_cost_sheet_dfs,
        dfs.get("기초재고_전체"),
        settlement_month,
    )

    if final_cost_df.empty:
        st.info("1번 기초 DB 데이터를 업로드하면 최종 원가 초안이 생성됩니다.")
        return final_cost_df

    if not purchase_cost_sheet_dfs:
        st.info("2-1 매입원가의 상품원장 데이터를 업로드하면 금액 컬럼이 채워집니다.")

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
# 7. 페이지 엔트리
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
    render_manufacturing_cost_upload(product_id_df, settlement_year, settlement_month)

    st.divider()
    st.header("3️⃣ 원가동인")

    st.divider()
    render_final_cost(product_id_df, purchase_cost_sheet_dfs, dfs, settlement_month)
