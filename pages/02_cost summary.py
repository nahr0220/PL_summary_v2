from io import BytesIO
import re

import numpy as np
import pandas as pd
import streamlit as st


st.set_page_config(page_title="손익분석", layout="wide")
st.title("Cost Summary")

tab1, tab2 = st.tabs(["VIEW", "UPLOAD"])


DETAIL_COLUMNS = [
    "신번호",
    "구번호",
    "차대번호",
    "차종",
    "차명",
    "반납일자",
    "매입일자",
    "분류1",
    "분류2",
    "분류3",
    "채널",
]
OUTPUT_DETAIL_COLUMNS = [*DETAIL_COLUMNS, "매입연도", "매입월"]
PRODUCT_ID_SOURCE_KEYS = [
    "매입조회",
    "검사매출",
    "정비매출",
    "기초재고",
    "전체상품조회",
    "위탁수불부_기초",
    "위탁수불부_입고",
]
PRODUCT_ID_COLUMNS = [
    "회계연도",
    "회계월",
    "상품ID",
    "기초재고",
    "정상입고",
    "타처입고",
    "당사/타사",
    "매출구분",
    "선매입여부",
    *OUTPUT_DETAIL_COLUMNS,
]
BASE_DF_KEYS = [
    "매입조회",
    "검사매출",
    "정비매출",
    "기초재고",
    "기초재고_전체",
    "전체상품조회",
    "위탁수불부",
    "위탁수불부_전체",
    "위탁수불부_기초",
    "위탁수불부_입고",
]
HIDDEN_BASE_DF_KEYS = ["기초재고_전체", "위탁수불부_전체"]


def _strip_columns(df):
    df = df.copy()
    df.columns = [str(column).strip() for column in df.columns]
    return df


# 매입조회
def preprocess_purchase_inquiry(file):
    df = _strip_columns(pd.read_excel(file))

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


def _ensure_product_id(df):
    df = df.copy()
    if "상품ID" not in df.columns and "차량아이디" in df.columns:
        df["상품ID"] = df["차량아이디"]
    if "상품ID" not in df.columns and "CODE" in df.columns:
        df["상품ID"] = df["CODE"]
    return df


def _is_flag_one(series):
    numeric_values = pd.to_numeric(series, errors="coerce")
    text_values = series.astype(str).str.strip()
    return numeric_values.eq(1) | text_values.eq("1")


# 전체상품조회
def preprocess_product_master(file):
    df = _strip_columns(pd.read_excel(file))
    df = _ensure_product_id(df)
    return df


# 위탁수불부
def preprocess_consignment_ledger(file, settlement_month):
    df = _strip_columns(pd.read_excel(file))
    df = _ensure_product_id(df)

    opening_column = f"{settlement_month}월기초_제외"
    inbound_column = f"{settlement_month}월입고_제외"

    if opening_column not in df.columns:
        raise KeyError(f"위탁수불부 파일에 '{opening_column}' 컬럼이 없습니다.")
    if inbound_column not in df.columns:
        raise KeyError(f"위탁수불부 파일에 '{inbound_column}' 컬럼이 없습니다.")

    opening_df = df[_is_flag_one(df[opening_column])].copy()
    inbound_df = df[_is_flag_one(df[inbound_column])].copy()

    return df, opening_df, inbound_df


# 검사/정비매출
def preprocess_sales(file):
    df = _strip_columns(pd.read_excel(file))
    df = _ensure_product_id(df)

    if "세부내역" in df.columns:
        df = df[~df["세부내역"].astype(str).str.contains("보증수리", na=False)].copy()

    if "상품ID" in df.columns:
        df = df.drop_duplicates(subset=["상품ID"]).copy()

    return df


# 기초재고
def preprocess_opening_inventory(file, base_df):
    df = _strip_columns(pd.read_excel(file))
    df = _ensure_product_id(df)

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


def _get_first_existing_column(df, candidates):
    for column in candidates:
        if column in df.columns:
            return df[column]
    return ""


def _build_detail_frame(df, date_column):
    columns = ["상품ID", *DETAIL_COLUMNS]

    if df is None or df.empty or "상품ID" not in df.columns:
        return pd.DataFrame(columns=columns)

    temp = _strip_columns(df)
    temp["상품ID"] = temp["상품ID"].astype(str).str.strip()
    temp = temp[temp["상품ID"] != ""].copy()

    if temp.empty:
        return pd.DataFrame(columns=columns)

    detail = pd.DataFrame({"상품ID": temp["상품ID"]})
    detail["신번호"] = _get_first_existing_column(temp, ["차량번호", "신번호"])
    detail["구번호"] = _get_first_existing_column(temp, ["이전차량번호", "구번호", "이전차량번호1"])
    detail["차대번호"] = _get_first_existing_column(temp, ["차대번호"])
    detail["차종"] = _get_first_existing_column(temp, ["차종"])
    detail["차명"] = _get_first_existing_column(temp, ["차명", "차량명"])
    detail["반납일자"] = _get_first_existing_column(temp, ["반납일자"])
    detail["매입일자"] = (
        "" if date_column is None else _get_first_existing_column(temp, [date_column])
    )
    detail["분류1"] = _get_first_existing_column(temp, ["신매입유형1", "매입유형-분류1"])
    detail["분류2"] = _get_first_existing_column(temp, ["신매입유형2", "매입유형-분류2"])
    detail["분류3"] = _get_first_existing_column(temp, ["신매입유형3", "매입유형-분류3"])
    detail["채널"] = _get_first_existing_column(temp, ["채널", "매입채널", "매입유형-분류4"])

    return detail.drop_duplicates(subset=["상품ID"], keep="first").reset_index(drop=True)


def _append_vehicle_details(merged, dfs):
    purchase_detail = _build_detail_frame(dfs.get("매입조회"), "계산서일자")
    inventory_detail = _build_detail_frame(dfs.get("기초재고"), "계산서일자")
    product_master_detail = _build_detail_frame(dfs.get("전체상품조회"), "매입세금계산서일자")
    consignment_ledger_detail = _build_detail_frame(dfs.get("위탁수불부_전체"), None)

    purchase_detail = purchase_detail.rename(
        columns={column: f"매입_{column}" for column in DETAIL_COLUMNS}
    )
    inventory_detail = inventory_detail.rename(
        columns={column: f"기초_{column}" for column in DETAIL_COLUMNS}
    )
    product_master_detail = product_master_detail.rename(
        columns={column: f"전체_{column}" for column in DETAIL_COLUMNS}
    )
    consignment_ledger_detail = consignment_ledger_detail.rename(
        columns={column: f"위탁수불_{column}" for column in DETAIL_COLUMNS}
    )

    merged = merged.merge(purchase_detail, on="상품ID", how="left")
    merged = merged.merge(inventory_detail, on="상품ID", how="left")
    merged = merged.merge(product_master_detail, on="상품ID", how="left")
    merged = merged.merge(consignment_ledger_detail, on="상품ID", how="left")

    use_inventory = merged["_출처"].eq("기초재고")
    no_detail_sales = merged["구분2"].isin(["검사매출", "정비매출"])
    helper_columns = []

    for column in DETAIL_COLUMNS:
        purchase_column = f"매입_{column}"
        inventory_column = f"기초_{column}"
        product_master_column = f"전체_{column}"
        consignment_ledger_column = f"위탁수불_{column}"
        helper_columns.extend(
            [
                purchase_column,
                inventory_column,
                product_master_column,
                consignment_ledger_column,
            ]
        )

        purchase_values = merged[purchase_column].replace("", pd.NA)
        inventory_values = merged[inventory_column].replace("", pd.NA)
        product_master_values = merged[product_master_column].replace("", pd.NA)
        consignment_ledger_values = merged[consignment_ledger_column].replace("", pd.NA)

        default_result = purchase_values.combine_first(inventory_values).combine_first(
            product_master_values
        ).combine_first(consignment_ledger_values)
        inventory_result = inventory_values.combine_first(consignment_ledger_values).combine_first(
            product_master_values
        ).combine_first(purchase_values)
        product_master_result = product_master_values.combine_first(purchase_values).combine_first(
            consignment_ledger_values
        ).combine_first(inventory_values)
        consignment_ledger_result = consignment_ledger_values.combine_first(
            product_master_values
        ).combine_first(purchase_values).combine_first(inventory_values)

        result = default_result
        result = result.where(~merged["_출처"].eq("전체상품조회"), product_master_result)
        result = result.where(
            ~merged["_출처"].isin(["위탁수불부_기초", "위탁수불부_입고"]),
            consignment_ledger_result,
        )
        result = result.where(~use_inventory, inventory_result)
        result = result.where(~no_detail_sales, "")
        merged[column] = result.fillna("")

    return merged.drop(columns=helper_columns)


def collect_product_ids(dfs, settlement_year=None, settlement_month=None):
    base_ids = []

    for key in PRODUCT_ID_SOURCE_KEYS:
        df = dfs.get(key)
        if df is None or df.empty or "상품ID" not in df.columns:
            continue

        temp = df[["상품ID"]].copy()
        temp["_출처"] = key
        temp["_입고구분"] = ""
        if key == "매입조회" and "입고구분" in df.columns:
            temp["_입고구분"] = df["입고구분"].astype(str).str.strip()

        temp["상품ID"] = temp["상품ID"].astype(str).str.strip()
        temp = temp[temp["상품ID"] != ""].copy()
        base_ids.append(temp)

    if not base_ids:
        return pd.DataFrame(columns=PRODUCT_ID_COLUMNS)

    merged = pd.concat(base_ids, ignore_index=True).reset_index(drop=True)

    for column in ["기초재고", "정상입고", "타처입고"]:
        merged[column] = 0
    merged["선매입여부"] = ""

    purchase_rows = merged["_출처"].eq("매입조회")
    merged.loc[merged["_출처"].eq("기초재고"), "기초재고"] = 1
    merged.loc[merged["_출처"].eq("위탁수불부_기초"), "기초재고"] = 1
    merged.loc[purchase_rows & merged["_입고구분"].eq("정상입고"), "정상입고"] = 1
    merged.loc[purchase_rows & merged["_입고구분"].eq("타처입고"), "타처입고"] = 1
    merged.loc[merged["_출처"].eq("전체상품조회"), "정상입고"] = 1
    merged.loc[merged["_출처"].eq("전체상품조회"), "선매입여부"] = "선매입"
    merged.loc[merged["_출처"].eq("위탁수불부_입고"), "정상입고"] = 1

    merged["구분2"] = ""
    internal_sales = merged[["기초재고", "정상입고", "타처입고"]].eq(1).any(axis=1)
    merged.loc[internal_sales, "구분2"] = "사내매출"
    merged.loc[
        merged["_출처"].isin(["위탁수불부_기초", "위탁수불부_입고"]), "구분2"
    ] = "위탁매출"
    merged.loc[merged["_출처"].eq("검사매출"), "구분2"] = "검사매출"
    merged.loc[merged["_출처"].eq("정비매출"), "구분2"] = "정비매출"
    merged["구분1"] = merged["구분2"].eq("사내매출").map({True: "당사차량", False: "타사차량"})

    merged = _append_vehicle_details(merged, dfs)
    parsed_purchase_date = pd.to_datetime(merged["매입일자"], format="mixed", errors="coerce")
    internal_sales = merged["구분2"].eq("사내매출")
    merged["매입연도"] = parsed_purchase_date.dt.year.astype("Int64").where(internal_sales, pd.NA)
    merged["매입월"] = parsed_purchase_date.dt.month.astype("Int64").where(internal_sales, pd.NA)
    merged["회계연도"] = int(settlement_year) if settlement_year is not None else pd.NA
    merged["회계월"] = int(settlement_month) if settlement_month is not None else pd.NA
    merged = merged.drop(columns=["_출처", "_입고구분"])
    merged = merged.rename(columns={"구분1": "당사/타사", "구분2": "매출구분"})
    merged = merged[
        [
            "회계연도",
            "회계월",
            "상품ID",
            "기초재고",
            "정상입고",
            "타처입고",
            "당사/타사",
            "매출구분",
            "선매입여부",
            *OUTPUT_DETAIL_COLUMNS,
        ]
    ]
    merged = merged.sort_values("상품ID").reset_index(drop=True)
    return merged


def dataframe_to_excel_bytes(df, sheet_name="Sheet1"):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
    output.seek(0)
    return output.getvalue()


def preprocess_cost_file(file):
    file.seek(0)
    sheets = pd.read_excel(file, sheet_name=None)
    processed_sheets = {}

    for sheet_name, df in sheets.items():
        df = _strip_columns(df)
        df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
        processed_sheets[sheet_name] = df

    return processed_sheets


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
        return "확인필요"
    else:
        match = re.search(r"\d{2,3}[^\d]\d{4}", text)
        return match.group() if match else ""


def _normalize_lookup_value(value):
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", "", str(value).strip())


def _build_detail_lookup(detail_df):
    empty_lookup = {
        "primary_vehicle_rows": [],
        "secondary_vehicle_rows": [],
        "product_type_by_id": {},
    }

    if detail_df is None or detail_df.empty:
        return empty_lookup

    detail = _strip_columns(detail_df)
    required_columns = ["신번호", "상품ID"]
    if any(column not in detail.columns for column in required_columns):
        return empty_lookup

    detail = detail.copy()
    detail["_신번호_lookup"] = detail["신번호"].apply(_normalize_lookup_value)
    detail["_구번호_lookup"] = ""
    if "구번호" in detail.columns:
        detail["_구번호_lookup"] = detail["구번호"].apply(_normalize_lookup_value)
    detail["_상품ID_lookup"] = detail["상품ID"].apply(_normalize_lookup_value)

    primary_vehicle_rows = (
        detail.loc[detail["_신번호_lookup"] != "", ["_신번호_lookup", "상품ID"]]
        .dropna(subset=["상품ID"])
        .values.tolist()
    )
    secondary_vehicle_rows = (
        detail.loc[detail["_구번호_lookup"] != "", ["_구번호_lookup", "상품ID"]]
        .dropna(subset=["상품ID"])
        .values.tolist()
    )

    product_type_by_id = {}
    if "분류1" in detail.columns:
        type_lookup = detail.loc[
            detail["_상품ID_lookup"] != "", ["_상품ID_lookup", "분류1"]
        ].drop_duplicates(subset=["_상품ID_lookup"], keep="first")
        product_type_by_id = dict(
            zip(type_lookup["_상품ID_lookup"], type_lookup["분류1"])
        )

    return {
        "primary_vehicle_rows": primary_vehicle_rows,
        "secondary_vehicle_rows": secondary_vehicle_rows,
        "product_type_by_id": product_type_by_id,
    }


def _lookup_product_id_in_lookup(lookup_car_number, detail_lookup):
    for detail_car_number, product_id in reversed(detail_lookup["primary_vehicle_rows"]):
        if lookup_car_number in detail_car_number:
            return product_id

    for detail_car_number, product_id in reversed(detail_lookup["secondary_vehicle_rows"]):
        if lookup_car_number in detail_car_number:
            return product_id

    return ""


def _lookup_product_id_by_car_number(car_number, detail_lookup, fallback_lookup=None):
    lookup_car_number = _normalize_lookup_value(car_number)
    if lookup_car_number == "":
        return ""

    product_id = _lookup_product_id_in_lookup(lookup_car_number, detail_lookup)
    if product_id != "" or fallback_lookup is None:
        return product_id

    return _lookup_product_id_in_lookup(lookup_car_number, fallback_lookup)


def _append_product_ledger_purchase_columns(df, detail_df, inventory_all_df=None):
    detail_lookup = _build_detail_lookup(detail_df)
    inventory_detail_df = _build_detail_frame(inventory_all_df, "위탁등록일자")
    inventory_lookup = _build_detail_lookup(inventory_detail_df)
    product_id_cache = {}
    product_ids = []

    for _, row in df.iterrows():
        status = str(row["상태"]).strip() if pd.notna(row["상태"]) else ""
        car_number = str(row["차량번호"]).strip() if pd.notna(row["차량번호"]) else ""

        if car_number == "지게차":
            summary_text = str(row["적요"]) if pd.notna(row["적요"]) else ""
            product_id = summary_text[:12]
        else:
            if car_number not in product_id_cache:
                product_id_cache[car_number] = _lookup_product_id_by_car_number(
                    car_number, detail_lookup, inventory_lookup
                )
            product_id = product_id_cache[car_number]

        product_ids.append(product_id)

    df["상품ID"] = product_ids

    product_type_by_id = inventory_lookup["product_type_by_id"].copy()
    product_type_by_id.update(detail_lookup["product_type_by_id"])
    purchase_types = []

    for _, row in df.iterrows():
        product_id = str(row["상품ID"]).strip() if pd.notna(row["상품ID"]) else ""
        purchase_type = product_type_by_id.get(_normalize_lookup_value(product_id), "")

        purchase_types.append(purchase_type)

    df["매입유형"] = purchase_types
    return df


# 상품원장
def preprocess_product_ledger(
    file,
    detail_df=None,
    inventory_all_df=None,
    settlement_year=None,
    settlement_month=None,
):
    file.seek(0)
    df = _strip_columns(pd.read_excel(file))
    df = df[~df["회계일자"].isin(["월계", "누계", "전일이월"])].copy()

    if "작성사원명" in df.columns:
        df = df[df["작성사원명"].astype(str).str.strip() != "김겸윤"].copy()

    df["회계일자"] = pd.to_datetime(df["회계일자"], format="mixed", errors="coerce")
    df = df[df["회계일자"].notna()].copy()

    df["회계연도"] = df["회계일자"].dt.year
    df["회계월"] = df["회계일자"].dt.month
    df["회계일자"] = df["회계일자"].dt.date

    if settlement_year is not None and settlement_month is not None:
        df = df[
            (df["회계연도"] == int(settlement_year))
            & (df["회계월"] == int(settlement_month))
        ].copy()

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
    df.loc[df["상태"].eq("") & df["원가구분"].eq("결산"), "상태"] = "결산"
    df.drop(columns=["abs_v", "seq"], inplace=True)
    df["금액"] = df["차변"] - df["대변"]

    if "잔액" in df.columns and "작성일자" in df.columns:
        columns_to_remove = df.loc[:, "잔액":"작성일자"].columns
        df.drop(columns=columns_to_remove, inplace=True)

    df = _append_product_ledger_purchase_columns(df, detail_df, inventory_all_df)
    return df


def _build_product_ledger_lookup(product_ledger_df):
    if (
        product_ledger_df is None
        or product_ledger_df.empty
        or "차량번호" not in product_ledger_df.columns
        or "상품ID" not in product_ledger_df.columns
    ):
        return {}

    ledger = _strip_columns(product_ledger_df)
    ledger = ledger.copy()
    ledger["_차량번호_lookup"] = ledger["차량번호"].apply(_normalize_lookup_value)
    ledger = ledger[ledger["_차량번호_lookup"] != ""].copy()

    if ledger.empty:
        return {}

    ledger = ledger.drop_duplicates(subset=["_차량번호_lookup"], keep="last")
    return dict(zip(ledger["_차량번호_lookup"], ledger["상품ID"]))


# 폐자원
def preprocess_waste_resource_file(file, product_ledger_df=None):
    file.seek(0)
    sheets = pd.read_excel(file, sheet_name=None)
    product_id_by_car_number = _build_product_ledger_lookup(product_ledger_df)
    processed_sheets = {}

    for sheet_name, df in sheets.items():
        df = _strip_columns(df)
        df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)

        if "구분" in df.columns:
            df = df[df["구분"].astype(str).str.strip().isin(["영수증", "계산서"])].copy()

        if "차량번호" in df.columns:
            df["상품ID"] = df["차량번호"].apply(
                lambda value: product_id_by_car_number.get(_normalize_lookup_value(value), "")
            )
        else:
            df["상품ID"] = ""

        if "매입세액공제액" in df.columns:
            tax_credit = pd.to_numeric(df["매입세액공제액"], errors="coerce").fillna(0)
            df["제외대상"] = np.where(tax_credit < 0, 1, 0)
        else:
            df["제외대상"] = 0

        if "매입일자" in df.columns:
            parsed_purchase_date = pd.to_datetime(
                df["매입일자"], format="mixed", errors="coerce"
            )
            df["회계연도"] = parsed_purchase_date.dt.year.astype("Int64")
            df["회계월"] = parsed_purchase_date.dt.month.astype("Int64")
        else:
            df["회계연도"] = pd.NA
            df["회계월"] = pd.NA

        ordered_columns = [
            column
            for column in ["제외대상", "회계연도", "회계월"]
            if column in df.columns
        ]
        remaining_columns = [column for column in df.columns if column not in ordered_columns]
        df = df[remaining_columns[:1] + ordered_columns + remaining_columns[1:]]

        processed_sheets[sheet_name] = df

    return processed_sheets


# 페이백
def _parse_year_month(value):
    if pd.isna(value):
        return pd.NA, pd.NA

    text = str(value).strip()
    match = re.search(r"(\d{4})\D?(\d{1,2})", text)
    if not match:
        return pd.NA, pd.NA

    return int(match.group(1)), int(match.group(2))


def preprocess_payback_file(
    file,
    detail_df=None,
    settlement_year=None,
    settlement_month=None,
):
    file.seek(0)
    sheets = pd.read_excel(file, sheet_name=None)
    detail_lookup = _build_detail_lookup(detail_df)
    processed_sheets = {}

    for sheet_name, df in sheets.items():
        df = _strip_columns(df)
        df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)

        if "연도월" in df.columns:
            parsed_period = df["연도월"].apply(_parse_year_month)
            df["연도"] = parsed_period.apply(lambda value: value[0]).astype("Int64")
            df["월"] = parsed_period.apply(lambda value: value[1]).astype("Int64")

            if settlement_year is not None and settlement_month is not None:
                df = df[
                    (df["연도"] == int(settlement_year))
                    & (df["월"] == int(settlement_month))
                ].copy()
        elif settlement_year is not None and settlement_month is not None:
            df = df.iloc[0:0].copy()

        original_product_ids = (
            df["상품ID"].copy()
            if "상품ID" in df.columns
            else pd.Series([""] * len(df), index=df.index)
        )

        if "차량번호" in df.columns:
            product_ids = []

            for index, row in df.iterrows():
                car_number = str(row["차량번호"]).strip() if pd.notna(row["차량번호"]) else ""

                if car_number == "지게차":
                    product_id = original_product_ids.loc[index]
                else:
                    product_id = _lookup_product_id_by_car_number(car_number, detail_lookup)

                product_ids.append(product_id)

            df["상품ID"] = product_ids
        elif "상품ID" not in df.columns:
            df["상품ID"] = ""

        processed_sheets[sheet_name] = df

    return processed_sheets


def _safe_excel_sheet_name(name, used_names):
    invalid_chars = ["\\", "/", "*", "?", ":", "[", "]"]
    safe_name = str(name).strip() or "Sheet"

    for char in invalid_chars:
        safe_name = safe_name.replace(char, "_")

    safe_name = safe_name[:31] or "Sheet"
    base_name = safe_name
    index = 1

    while safe_name in used_names:
        index += 1
        suffix = f"_{index}"
        safe_name = f"{base_name[:31 - len(suffix)]}{suffix}"

    used_names.add(safe_name)
    return safe_name


def workbook_to_excel_bytes(sheet_dfs):
    output = BytesIO()
    used_sheet_names = set()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in sheet_dfs.items():
            safe_sheet_name = _safe_excel_sheet_name(sheet_name, used_sheet_names)
            df.to_excel(writer, index=False, sheet_name=safe_sheet_name)

    output.seek(0)
    return output.getvalue()


def dataframe_for_display(df):
    display_df = df.copy()
    display_df.columns = [str(column) for column in display_df.columns]

    for column in display_df.columns:
        if (
            pd.api.types.is_object_dtype(display_df[column])
            or pd.api.types.is_string_dtype(display_df[column])
        ):
            display_df[column] = display_df[column].apply(
                lambda value: "" if pd.isna(value) else str(value)
            )

    return display_df


def empty_product_id_df():
    return pd.DataFrame(columns=PRODUCT_ID_COLUMNS)


def render_settlement_selector():
    if "settlement_year" not in st.session_state:
        st.session_state["settlement_year"] = pd.Timestamp.today().year
    if "settlement_month" not in st.session_state:
        st.session_state["settlement_month"] = pd.Timestamp.today().month

    year_col, month_col, spacer_col, button_col = st.columns([1, 1, 1, 0.6])

    with year_col:
        selected_year = st.number_input(
            "결산연도",
            min_value=2000,
            max_value=2100,
            value=int(st.session_state["settlement_year"]),
            step=1,
            key="selected_settlement_year",
        )

    with month_col:
        selected_month = st.selectbox(
            "결산월",
            options=list(range(1, 13)),
            index=st.session_state["settlement_month"] - 1,
            format_func=lambda month: f"{month}월",
            key="selected_settlement_month",
        )

    with button_col:
        st.write("")
        apply_period = st.button(
            "결산연/월 적용",
            key="apply_settlement_period",
            use_container_width=True,
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


def initialize_base_dfs():
    return {key: None for key in BASE_DF_KEYS}


def process_base_file(fname, file, dfs, settlement_month):
    if "매입조회" in fname:
        dfs["매입조회"] = preprocess_purchase_inquiry(file)
    elif "전체상품조회" in fname:
        dfs["전체상품조회"] = preprocess_product_master(file)
    elif "위탁수불부" in fname:
        (
            consignment_ledger_all,
            consignment_ledger_opening,
            consignment_ledger_inbound,
        ) = preprocess_consignment_ledger(file, settlement_month)
        dfs["위탁수불부"] = consignment_ledger_all
        dfs["위탁수불부_전체"] = consignment_ledger_all
        dfs["위탁수불부_기초"] = consignment_ledger_opening
        dfs["위탁수불부_입고"] = consignment_ledger_inbound
    elif "검사매출" in fname:
        dfs["검사매출"] = preprocess_sales(file)
    elif "정비매출" in fname:
        dfs["정비매출"] = preprocess_sales(file)


def process_opening_inventory_files(file_map, dfs):
    for fname, file in file_map.items():
        if "기초재고" not in fname:
            continue

        try:
            inventory_all, inventory_filtered = preprocess_opening_inventory(
                file, dfs["매입조회"]
            )
            dfs["기초재고_전체"] = inventory_all
            dfs["기초재고"] = inventory_filtered
            dfs["매입조회"] = filter_purchase_inquiry(
                dfs["매입조회"], dfs["기초재고_전체"]
            )
        except Exception as exc:
            st.error(f"{fname} 처리 중 오류: {exc}")


def render_dataframe_tabs(sheet_dfs):
    tabs = st.tabs(list(sheet_dfs.keys()))
    for i, sheet_name in enumerate(sheet_dfs.keys()):
        with tabs[i]:
            current_df = sheet_dfs[sheet_name]
            st.write(f"건수: {len(current_df):,}건")
            st.dataframe(dataframe_for_display(current_df), width='stretch')


def _unique_sheet_label(sheet_dfs, label):
    label = str(label).strip() or "Sheet"
    unique_label = label
    index = 2

    while unique_label in sheet_dfs:
        unique_label = f"{label}_{index}"
        index += 1

    return unique_label


def render_base_upload(settlement_year, settlement_month):
    st.header("1️⃣ 기초 DB")

    uploaded_files = st.file_uploader(
        "파일 업로드하세요.",
        type=["xlsx"],
        accept_multiple_files=True,
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


def preprocess_purchase_cost_files(
    uploaded_cost_files,
    product_id_df,
    dfs,
    settlement_year,
    settlement_month,
):
    cost_sheet_dfs = {}
    product_ledger_frames = []

    for file in uploaded_cost_files:
        if "상품원장" not in file.name:
            continue

        try:
            product_ledger_df = preprocess_product_ledger(
                file,
                product_id_df,
                dfs.get("기초재고_전체"),
                settlement_year,
                settlement_month,
            )
            cost_sheet_dfs[_unique_sheet_label(cost_sheet_dfs, "상품원장")] = product_ledger_df
            product_ledger_frames.append(product_ledger_df)
        except Exception as exc:
            st.error(f"{file.name} 처리 중 오류: {exc}")

    product_ledger_lookup_df = (
        pd.concat(product_ledger_frames, ignore_index=True)
        if product_ledger_frames
        else pd.DataFrame()
    )

    for file in uploaded_cost_files:
        if "상품원장" in file.name:
            continue

        try:
            file_label = file.name.rsplit(".", 1)[0]

            if "폐자원" in file.name:
                if product_ledger_lookup_df.empty:
                    st.warning("폐자원 파일의 상품ID를 가져오려면 상품원장 파일도 함께 업로드하세요.")
                file_sheets = preprocess_waste_resource_file(file, product_ledger_lookup_df)
                display_label = "폐자원공제"
            elif "페이백" in file.name:
                if product_id_df.empty:
                    st.warning("페이백 파일의 상품ID를 가져오려면 1번 기초 DB 파일도 함께 업로드하세요.")
                file_sheets = preprocess_payback_file(
                    file,
                    product_id_df,
                    settlement_year,
                    settlement_month,
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


def render_purchase_cost_upload(product_id_df, dfs, settlement_year, settlement_month):
    st.subheader("2-1. 매입원가")

    uploaded_cost_files = st.file_uploader(
        "매입원가 파일을 업로드하세요.",
        type=["xlsx", "xls"],
        accept_multiple_files=True,
        key="cost_files",
    )

    if not uploaded_cost_files:
        return {}

    if len(uploaded_cost_files) > 3:
        st.warning("원가 파일은 3개까지 업로드하는 기준으로 처리합니다.")

    cost_sheet_dfs = preprocess_purchase_cost_files(
        uploaded_cost_files,
        product_id_df,
        dfs,
        settlement_year,
        settlement_month,
    )
    render_sheet_workbook(
        cost_sheet_dfs,
        "매입원가 전처리 파일 다운로드",
        "purchase_cost_preprocessed.xlsx",
        "매입원가 파일에서 표시할 데이터가 없습니다.",
    )
    return cost_sheet_dfs


def render_manufacturing_cost_upload():
    st.subheader("2-2. 제조원가")

    uploaded_files = st.file_uploader(
        "제조원가 파일을 업로드하세요.",
        type=["xlsx", "xls"],
        accept_multiple_files=True,
        key="manufacturing_cost_files",
    )

    manufacturing_cost_sheet_dfs = {}
    if uploaded_files:
        for file in uploaded_files:
            try:
                file_label = file.name.rsplit(".", 1)[0]
                file_sheets = preprocess_cost_file(file)

                for sheet_name, df in file_sheets.items():
                    manufacturing_cost_sheet_dfs[f"{file_label}_{sheet_name}"] = df
            except Exception as exc:
                st.error(f"{file.name} 처리 중 오류: {exc}")

        render_sheet_workbook(
            manufacturing_cost_sheet_dfs,
            "제조원가 전처리 파일 다운로드",
            "manufacturing_cost_preprocessed.xlsx",
            "제조원가 파일에서 표시할 데이터가 없습니다.",
        )

    return manufacturing_cost_sheet_dfs


with tab1:
    st.write("준비 중")


with tab2:
    settlement_year, settlement_month = render_settlement_selector()
    dfs, product_id_df = render_base_upload(settlement_year, settlement_month)

    st.divider()
    st.header("2️⃣ 원가")
    render_purchase_cost_upload(product_id_df, dfs, settlement_year, settlement_month)

    st.divider()
    render_manufacturing_cost_upload()

    st.divider()
    st.header("3️⃣ 원가동인")

    st.divider()
    st.header("4️⃣ 최종 원가 생성")
