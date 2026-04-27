from io import BytesIO
import pandas as pd
import streamlit as st


st.set_page_config(page_title="손익분석", layout="wide")
st.title("Cost Summary")

tab1, tab2 = st.tabs(["VIEW", "UPLOAD"])


def preprocess_purchase_inquiry(file):
    df = pd.read_excel(file, header=1).copy()

    if "상품ID" not in df.columns and "차량아이디" in df.columns:
        df["상품ID"] = df["차량아이디"]

    if "회계월" not in df.columns:
        if "계산서일자" in df.columns:
            parsed_month = pd.to_datetime(
                df["계산서일자"], format="mixed", errors="coerce"
            ).dt.month

            if parsed_month.isna().all():
                parsed_month = pd.to_numeric(df["계산서일자"], errors="coerce")

            df["회계월"] = parsed_month

    df["입고구분"] = df["매입유형-분류1"].apply(
        lambda x: "타처입고" if str(x).strip() == "자산" else "정상입고"
    )

    return df


def preprocess_consignment(file):
    df = pd.read_excel(file).copy()
    df.columns = [str(column).strip() for column in df.columns]
    df = df[~df["거래처명"].isin(["오토플러스서비스(주)", "테슬라코리아유한회사"])].copy()
    return df


def preprocess_sales(file):
    df = pd.read_excel(file).copy()

    if "상품ID" not in df.columns and "차량아이디" in df.columns:
        df["상품ID"] = df["차량아이디"]

    if "상품ID" in df.columns:
        df = df.drop_duplicates(subset=["상품ID"]).copy()

    return df


def preprocess_opening_inventory(file, base_df):
    df = pd.read_excel(file, sheet_name="기초재고").copy()
    if "상품ID" not in df.columns and "CODE" in df.columns:
        df["상품ID"] = df["CODE"]

    if base_df is None or base_df.empty:
        raise ValueError("기초재고 처리 전에 매입조회 파일이 먼저 정상 로드되어야 합니다.")

    if "회계월" not in base_df.columns or base_df["회계월"].dropna().empty:
        raise KeyError("매입조회 파일에 '회계월' 컬럼이 없습니다.")

    current_month = int(base_df["회계월"].dropna().iloc[0])
    previous_month = 12 if current_month == 1 else current_month - 1
    col_name = f"{previous_month}월 기말여부"

    if col_name not in df.columns:
        raise KeyError(f"기초재고 파일에 '{col_name}' 컬럼이 없습니다.")

    filtered_df = df[df[col_name] == 1].copy()
    return df, filtered_df


def filter_purchase_inquiry(df_purchase, df_inventory_all):
    if df_purchase is None or df_purchase.empty:
        return df_purchase

    df_purchase = df_purchase.copy()

    if df_inventory_all is None or df_inventory_all.empty:
        return df_purchase

    required_columns = ["상품ID", "선매입 여부"]
    if any(column not in df_inventory_all.columns for column in required_columns):
        return df_purchase

    if "상품ID" not in df_purchase.columns:
        return df_purchase

    excluded_ids = (
        df_inventory_all.loc[df_inventory_all["선매입 여부"] == 1, "상품ID"]
        .dropna()
        .astype(str)
        .str.strip()
    )
    excluded_ids = set(excluded_ids)

    purchase_ids = df_purchase["상품ID"].fillna("").astype(str).str.strip()
    df_purchase = df_purchase[~purchase_ids.isin(excluded_ids)].copy()

    return df_purchase


def _build_flag_frame(df, flag_name, condition=None):
    if df is None or df.empty or "상품ID" not in df.columns:
        return pd.DataFrame(columns=["상품ID", flag_name])

    temp = df.copy()
    temp["상품ID"] = temp["상품ID"].astype(str).str.strip()
    temp = temp[temp["상품ID"] != ""].copy()

    if condition is None:
        temp = temp[["상품ID"]].drop_duplicates().copy()
        temp[flag_name] = 1
        return temp

    filtered = temp.loc[condition(temp), ["상품ID"]].drop_duplicates().copy()
    filtered[flag_name] = 1
    return filtered


def collect_product_ids(dfs):
    base_ids = []

    for key in ["매입조회", "검사매출", "정비매출", "기초재고", "위탁조회"]:
        df = dfs.get(key)
        if df is None or df.empty or "상품ID" not in df.columns:
            continue

        temp = df[["상품ID"]].copy()
        temp["상품ID"] = temp["상품ID"].astype(str).str.strip()
        temp = temp[temp["상품ID"] != ""].copy()
        base_ids.append(temp)

    if not base_ids:
        return pd.DataFrame(
            columns=["상품ID", "기초재고", "정상입고", "타처입고", "구분1", "구분2"]
        )

    merged = pd.concat(base_ids, ignore_index=True).reset_index(drop=True)

    inventory_flag = _build_flag_frame(dfs.get("기초재고"), "기초재고")
    normal_flag = _build_flag_frame(
        dfs.get("매입조회"),
        "정상입고",
        condition=lambda frame: frame["입고구분"].astype(str).str.strip() == "정상입고",
    )
    external_flag = _build_flag_frame(
        dfs.get("매입조회"),
        "타처입고",
        condition=lambda frame: frame["입고구분"].astype(str).str.strip() == "타처입고",
    )
    consignment_flag = _build_flag_frame(dfs.get("위탁조회"), "위탁매출")
    inspection_flag = _build_flag_frame(dfs.get("검사매출"), "검사매출")
    maintenance_flag = _build_flag_frame(dfs.get("정비매출"), "정비매출")

    flag_frames = [
        inventory_flag,
        normal_flag,
        external_flag,
        consignment_flag,
        inspection_flag,
        maintenance_flag,
    ]
    for flag_df in flag_frames:
        merged = merged.merge(flag_df, on="상품ID", how="left")

    flag_columns = ["기초재고", "정상입고", "타처입고", "위탁매출", "검사매출", "정비매출"]
    for column in flag_columns:
        merged[column] = merged[column].fillna(0).astype(int)

    merged["구분2"] = ""
    internal_sales = merged[["기초재고", "정상입고", "타처입고"]].eq(1).any(axis=1)
    merged.loc[internal_sales, "구분2"] = "사내매출"
    merged.loc[(merged["구분2"] == "") & (merged["위탁매출"] == 1), "구분2"] = "위탁매출"
    merged.loc[(merged["구분2"] == "") & (merged["검사매출"] == 1), "구분2"] = "검사매출"
    merged.loc[(merged["구분2"] == "") & (merged["정비매출"] == 1), "구분2"] = "정비매출"
    merged["구분1"] = merged["구분2"].eq("사내매출").map({True: "당사차량", False: "타사차량"})

    merged = merged.drop(columns=["위탁매출", "검사매출", "정비매출"])
    merged = merged[["상품ID", "기초재고", "정상입고", "타처입고", "구분1", "구분2"]]
    merged = merged.sort_values("상품ID").reset_index(drop=True)
    return merged


def dataframe_to_excel_bytes(df, sheet_name="Sheet1"):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
    output.seek(0)
    return output.getvalue()


with tab1:
    st.write("준비 중")


with tab2:
    st.header("1️⃣ 기초 DB")

    uploaded_files = st.file_uploader(
        "파일 업로드하세요.",
        type=["xlsx"],
        accept_multiple_files=True,
    )

    dfs = {
        "매입조회": None,
        "검사매출": None,
        "정비매출": None,
        "기초재고": None,
        "기초재고_전체": None,
        "위탁조회": None,
    }

    if uploaded_files:
        file_map = {file.name: file for file in uploaded_files}

        for fname, file in file_map.items():
            try:
                if "매입조회" in fname:
                    dfs["매입조회"] = preprocess_purchase_inquiry(file)

                elif "위탁조회" in fname:
                    dfs["위탁조회"] = preprocess_consignment(file)

                elif "검사매출" in fname:
                    dfs["검사매출"] = preprocess_sales(file)

                elif "정비매출" in fname:
                    dfs["정비매출"] = preprocess_sales(file)

            except Exception as exc:
                st.error(f"{fname} 처리 중 오류: {exc}")

        for fname, file in file_map.items():
            if "기초재고" not in fname:
                continue

            try:
                df_inventory_all, df_inventory_filtered = preprocess_opening_inventory(
                    file, dfs["매입조회"]
                )
                dfs["기초재고_전체"] = df_inventory_all
                dfs["기초재고"] = df_inventory_filtered
                dfs["매입조회"] = filter_purchase_inquiry(
                    dfs["매입조회"], dfs["기초재고_전체"]
                )

            except Exception as exc:
                st.error(f"{fname} 처리 중 오류: {exc}")

        st.divider()
        st.subheader("🧾 상품ID 모음")
        product_id_df = collect_product_ids(dfs)

        if not product_id_df.empty:
            unique_product_id_df = product_id_df[["상품ID"]].copy()
            st.write(f"상품ID 개수: {len(unique_product_id_df):,}건")
            # st.dataframe(unique_product_id_df, use_container_width=True)

            with st.expander("구분 포함 상세 보기"):
                st.download_button(
                    "엑셀 다운로드",
                    data=dataframe_to_excel_bytes(product_id_df, sheet_name="구분포함상세"),
                    file_name="product_id_detail.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                st.dataframe(product_id_df, use_container_width=True)
        else:
            st.info("상품ID를 가진 업로드 데이터가 아직 없습니다.")

    visible_dfs = {
        key: value
        for key, value in dfs.items()
        if key != "기초재고_전체" and value is not None
    }
    if visible_dfs:
        with st.expander("파일별 개별 데이터 확인"):
            active_tabs = list(visible_dfs.keys())
            tabs = st.tabs(active_tabs)
            for i, tab_name in enumerate(active_tabs):
                with tabs[i]:
                    current_df = visible_dfs[tab_name]
                    st.write(f"건수: {len(current_df):,}건")
                    st.dataframe(current_df, use_container_width=True)

    st.divider()
    st.header("2️⃣ 원가")

    st.divider()
    st.header("3️⃣ 원가동인")

    st.divider()
    st.header("4️⃣ 최종 원가 생성")

