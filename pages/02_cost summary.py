import re
import numpy as np
import pandas as pd
import streamlit as st


st.set_page_config(page_title="손익분석", layout="wide")
st.title("Cost Summary")

tab1, tab2 = st.tabs(["VIEW", "UPLOAD"])


def extract_reference(text):
    text = str(text) if pd.notna(text) else ""
    patterns = [
        r"C\d{11}_\d{2,3}[^\d]\d{4}",
        r"C\d{11}_[^\d]{3}",
        r"C\d{11}_[^\d]{2}\d{2,3}[^\d]\d{4}",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group()
    return ""


def classify_cost(row):
    text = str(row["적요"]) if pd.notna(row["적요"]) else ""
    reference = row["참고"]

    def contains_any(keywords):
        return any(keyword in text for keyword in keywords)

    if contains_any(["매출원가", "재공품", "상품평가충당금"]):
        return "결산"
    elif contains_any(["오류"]):
        return "매입수수료"
    elif contains_any(["초과운행"]):
        return "초과운행"
    elif contains_any(["계약만기 도래분(반납)"]):
        return "페이백(반납)"
    elif contains_any(["계약만기 도래분(미반납)"]):
        return "페이백(미반납)"
    elif contains_any(["폐자원"]):
        return "폐자원공제"
    elif contains_any(["취득세", "취등록세"]):
        return "취득세"
    elif contains_any(["선매입"]):
        return "상품매입액"
    elif contains_any(
        [
            "피알앤디컴퍼니",
            "경매장",
            "인품",
            "엔카",
            "중개",
            "알선",
            "매입",
            "소개수수료",
            "헤이딜러",
            "매입수수료",
            "매입 수수료",
            "낙찰수수료",
            "낙찰 수수료",
        ]
    ):
        return "매입수수료"
    elif contains_any(["(상품->건설중인자산)", "상품->자산"]):
        return "자산출고"
    elif contains_any(["상품전환"]):
        return "타처입고"
    elif pd.notna(reference) and reference not in ["", 0]:
        return "상품매입액"
    else:
        return ""


def extract_car_number(row):
    text = str(row["적요"]) if pd.notna(row["적요"]) else ""
    cost_type = row["원가구분"]
    if cost_type == "결산":
        return "결산"
    elif text.endswith("지게차"):
        return "지게차"
    elif cost_type in ["페이백(반납)", "페이백(미반납)", "폐자원공제"]:
        return ""
    else:
        match = re.search(r"\d{2,3}[^\d]\d{4}", text)
        return match.group() if match else ""


def preprocess_product_ledger(file):
    df = pd.read_excel(file)
    df = df[~df["회계일자"].isin(["월계", "누계", "전일이월"])].copy()
    df["회계일자"] = pd.to_datetime(df["회계일자"], format="mixed", errors="coerce")
    df = df[df["회계일자"].notna()].copy()

    df["회계연도"] = df["회계일자"].dt.year
    df["회계월"] = df["회계일자"].dt.month
    df["회계일자"] = df["회계일자"].dt.date

    df["참고"] = df["적요"].apply(extract_reference)
    df["원가구분"] = df.apply(classify_cost, axis=1)
    df["차량번호"] = df.apply(extract_car_number, axis=1)

    df["차변"] = pd.to_numeric(df["차변"], errors="coerce").fillna(0)
    df["대변"] = pd.to_numeric(df["대변"], errors="coerce").fillna(0)
    df["abs_v"] = df["차변"].abs()
    df["seq"] = df.groupby(
        ["회계연도", "회계월", "차량번호", "abs_v", df["차변"] > 0]
    ).cumcount()

    canceled = (
        df.groupby(["회계연도", "회계월", "차량번호", "abs_v", "seq"])["차변"]
        .transform("count")
        > 1
    )
    df["상태"] = np.where(canceled, "취소", "")
    df.drop(columns=["abs_v", "seq"], inplace=True)
    df["금액"] = df["차변"] - df["대변"]

    if "잔액" in df.columns and "작성일자" in df.columns:
        columns_to_remove = df.loc[:, "잔액":"작성일자"].columns
        df.drop(columns=columns_to_remove, inplace=True)

    return df


def preprocess_consignment(file):
    df = pd.read_excel(file)
    df = df[~df["거래처명"].isin(["오토플러스서비스(주)", "테슬라코리아유한회사"])].copy()
    return df


def preprocess_sales(file):
    df = pd.read_excel(file)
    if "상품ID" not in df.columns and "차량아이디" in df.columns:
        df["상품ID"] = df["차량아이디"]
    df = df.drop_duplicates(subset=["상품ID"]).copy()
    return df


def preprocess_opening_inventory(file, df_base):
    df = pd.read_excel(file, sheet_name="기초DB", header=1)

    if df_base is None or df_base.empty:
        raise ValueError("기초재고 처리 전에 상품원장 파일이 먼저 정상 로드되어야 합니다.")

    current_month = int(df_base["회계월"].dropna().iloc[0])
    previous_month = 12 if current_month == 1 else current_month - 1
    col_name = f"{previous_month}월 기말여부"

    if col_name not in df.columns:
        raise KeyError(f"기초재고 파일에 '{col_name}' 컬럼이 없습니다.")

    filtered_df = df[df[col_name] == 1].copy()
    return df.copy(), filtered_df


def add_purchase_type_from_inventory(df_base, df_inventory):
    if df_base is None or df_base.empty:
        return df_base

    df_base = df_base.copy()

    if df_inventory is None or df_inventory.empty:
        df_base["매입유형"] = "확인필요"
        return df_base

    required_columns = ["신번호", "매입유형"]
    if any(column not in df_inventory.columns for column in required_columns):
        df_base["매입유형"] = "확인필요"
        return df_base

    lookup = (
        df_inventory[["신번호", "매입유형"]]
        .dropna(subset=["신번호"])
        .drop_duplicates(subset=["신번호"], keep="first")
        .set_index("신번호")["매입유형"]
    )

    df_base["매입유형"] = df_base["차량번호"].map(lookup)
    df_base["매입유형"] = df_base["매입유형"].fillna("확인필요")

    return df_base


def add_product_id_from_inventory(df_base, df_inventory):
    if df_base is None or df_base.empty:
        return df_base

    df_base = df_base.copy()

    if "적요" not in df_base.columns:
        df_base["상품ID"] = "확인필요"
        return df_base

    if "상태" not in df_base.columns:
        df_base["상태"] = ""

    # 결산은 현재 원가구분 컬럼에 들어가므로 함께 반영합니다.
    settlement_mask = df_base.get("원가구분", pd.Series("", index=df_base.index)) == "결산"
    canceled_mask = df_base["상태"] == "취소"

    extracted_product_id = df_base["적요"].astype(str).str.extract(r"(C\d{11})", expand=False)

    lookup_key = (
        df_base["차량번호"].fillna("").astype(str)
        + "_"
        + df_base["매입유형"].fillna("").astype(str)
    )

    inventory_product_id = pd.Series(index=df_base.index, dtype="object")
    required_columns = ["상품ID", "신번호", "매입유형"]
    if df_inventory is not None and not df_inventory.empty:
        if all(column in df_inventory.columns for column in required_columns):
            inventory_lookup = df_inventory[required_columns].copy()
            inventory_lookup["lookup_key"] = (
                inventory_lookup["신번호"].fillna("").astype(str)
                + "_"
                + inventory_lookup["매입유형"].fillna("").astype(str)
            )
            inventory_lookup = inventory_lookup.drop_duplicates(subset=["lookup_key"], keep="last")
            inventory_lookup = inventory_lookup.set_index("lookup_key")["상품ID"]
            inventory_product_id = lookup_key.map(inventory_lookup)

    df_base["상품ID"] = extracted_product_id
    df_base["상품ID"] = df_base["상품ID"].fillna(inventory_product_id)
    df_base["상품ID"] = df_base["상품ID"].fillna("확인필요")
    df_base.loc[canceled_mask, "상품ID"] = "취소"
    df_base.loc[settlement_mask, "상품ID"] = "결산"

    return df_base


# with tab1:  # view (비용요약정보)


with tab2:  # upload (비용요약정보 업로드)
    st.header("1️⃣ 기초 DB")

    uploaded_files = st.file_uploader(
        "파일 업로드하세요.",
        type=["xlsx"],
        accept_multiple_files=True,
    )

    dfs = {
        "상품원장": None,
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
                if "상품원장" in fname:
                    dfs["상품원장"] = preprocess_product_ledger(file)

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
                    file, dfs["상품원장"]
                )
                dfs["기초재고_전체"] = df_inventory_all
                dfs["기초재고"] = df_inventory_filtered
                dfs["상품원장"] = add_purchase_type_from_inventory(
                    dfs["상품원장"], dfs["기초재고_전체"]
                )
                dfs["상품원장"] = add_product_id_from_inventory(
                    dfs["상품원장"], dfs["기초재고_전체"]
                )
            except Exception as exc:
                st.error(f"{fname} 처리 중 오류: {exc}")

        # if dfs["상품원장"] is not None:
        #     st.dataframe(dfs["상품원장"], use_container_width=True)

        st.divider()
        st.subheader("🔗 데이터 결합 결과")

        if dfs["상품원장"] is not None:
            final_df = dfs["상품원장"].copy()

            if dfs["검사매출"] is not None:
                pass

            if dfs["위탁조회"] is not None:
                pass

        else:
            st.warning("기준이 되는 '상품원장' 파일을 먼저 업로드해주세요.")

    if any(value is not None for value in dfs.values()):
        with st.expander("파일별 개별 데이터 확인"):
            active_tabs = [key for key, value in dfs.items() if value is not None]
            tabs = st.tabs(active_tabs)
            for i, tab_name in enumerate(active_tabs):
                with tabs[i]:
                    st.dataframe(dfs[tab_name], use_container_width=True)

    st.divider()
    st.header("2️⃣ 원가")

    st.divider()
    st.header("3️⃣ 원가동인")

    st.divider()
    st.header("4️⃣ 최종 원가 생성")
