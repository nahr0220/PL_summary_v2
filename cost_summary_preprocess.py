"""손익분석 - 전처리 모듈

업로드된 파일들을 DataFrame 으로 변환하고 상품ID 통합까지 책임진다.

구성:
    1. 상수 정의
    2. 공통 헬퍼 (DataFrame / lookup / 엑셀 I/O)
    3. 전처리 - 기초 DB
    4. 차량 상세 / lookup 빌더
    5. 상품ID 모음 (collect_product_ids)
    6. 전처리 - 매입원가 (상품원장 / 폐자원 / 페이백 / 일반)
    7. 전처리 - 제조원가 (재료비 / 노무비·경비 / 직접경비)
"""

from io import BytesIO
import re

import numpy as np
import pandas as pd


# ============================================================
# 1. 상수 정의
# ============================================================

DETAIL_COLUMNS = [
    "신번호", "구번호", "차대번호", "차종", "차명",
    "반납일자", "매입일자", "분류1", "분류2", "분류3", "채널",
]
OUTPUT_DETAIL_COLUMNS = [*DETAIL_COLUMNS, "매입연도", "매입월"]

PRODUCT_ID_SOURCE_KEYS = [
    "매입조회", "검사매출", "정비매출", "기초재고",
    "전체상품조회", "위탁수불부_기초", "위탁수불부_입고",
]
PRODUCT_ID_COLUMNS = [
    "회계연도", "회계월", "상품ID",
    "기초재고", "정상입고", "타처입고",
    "당사/타사", "매출구분", "선매입여부",
    *OUTPUT_DETAIL_COLUMNS,
]

# 금액 컬럼
PURCHASE_AMOUNT_COLUMNS = ["상품매입액", "취득세", "매입수수료"]
WASTE_RESOURCE_COLUMN = "폐자원공제"
PAYBACK_RETURN_COLUMN = "페이백(반납)"
PAYBACK_UNRETURNED_COLUMN = "페이백(미반납)"
EXCESS_DRIVING_COLUMN = "초과운행"
DIFFERENCE_ALLOCATION_COLUMN = "차액배부"
TOTAL_PURCHASE_COST_COLUMN = "매입원가"  # FINAL_COST_MONTHLY_COLUMNS 8개 합계용

FINAL_COST_AMOUNT_COLUMNS = [
    *PURCHASE_AMOUNT_COLUMNS,
    WASTE_RESOURCE_COLUMN,
    PAYBACK_RETURN_COLUMN,
    PAYBACK_UNRETURNED_COLUMN,
    EXCESS_DRIVING_COLUMN,
]
FINAL_COST_MONTHLY_COLUMNS = [
    *PURCHASE_AMOUNT_COLUMNS,
    WASTE_RESOURCE_COLUMN,
    DIFFERENCE_ALLOCATION_COLUMN,
    PAYBACK_RETURN_COLUMN,
    PAYBACK_UNRETURNED_COLUMN,
    EXCESS_DRIVING_COLUMN,
]
DIFFERENCE_SOURCE_COST_COLUMNS = ["취득세", "매입수수료", WASTE_RESOURCE_COLUMN]
PRODUCT_LEDGER_TOTAL_COST_COLUMNS = [
    *DIFFERENCE_SOURCE_COST_COLUMNS,
    PAYBACK_RETURN_COLUMN,
    PAYBACK_UNRETURNED_COLUMN,
    EXCESS_DRIVING_COLUMN,
]

# 시트 컬럼 후보 (시트마다 표기가 다른 경우)
WASTE_RESOURCE_AMOUNT_COLUMNS = ["매입세금공제액", "매입세액공제액", "매입세액공제"]
PAYBACK_RETURN_AMOUNT_COLUMNS = ["선수수익(VAT外)", "선수수익(VAT외)", "선수수익(VAT 外)"]

MATERIAL_SALES_COLUMNS = ["정비매출", "사내매출", "위탁매출"]

# 재료비 시트 금액 컬럼 후보 (시트마다 표기가 다를 수 있음)
MATERIAL_COST_AMOUNT_COLUMNS = ["금액", "출고금액", "출고가액", "원가", "재료비"]

# 기초 DB DataFrame 키
BASE_DF_KEYS = [
    "매입조회", "검사매출", "정비매출",
    "기초재고", "기초재고_전체",
    "전체상품조회",
    "위탁수불부", "위탁수불부_전체", "위탁수불부_기초", "위탁수불부_입고",
]
HIDDEN_BASE_DF_KEYS = ["기초재고_전체", "위탁수불부_전체"]


# ============================================================
# 2. 공통 헬퍼
# ============================================================

# ----- DataFrame 기본 처리 -----

def _strip_columns(df):
    """컬럼명 좌우 공백 제거."""
    df = df.copy()
    df.columns = [str(column).strip() for column in df.columns]
    return df


def _is_flag_one(series):
    """숫자 1 또는 문자열 '1'인지 확인."""
    numeric_values = pd.to_numeric(series, errors="coerce")
    text_values = series.astype(str).str.strip()
    return numeric_values.eq(1) | text_values.eq("1")


def _ensure_product_id(df):
    """상품ID 컬럼이 없으면 차량아이디 / CODE 에서 생성."""
    df = df.copy()
    if "상품ID" not in df.columns and "차량아이디" in df.columns:
        df["상품ID"] = df["차량아이디"]
    if "상품ID" not in df.columns and "CODE" in df.columns:
        df["상품ID"] = df["CODE"]
    return df


def _get_first_existing_column(df, candidates):
    """후보 컬럼 중 가장 먼저 존재하는 컬럼 반환. 없으면 빈 문자열."""
    for column in candidates:
        if column in df.columns:
            return df[column]
    return ""


def _normalize_lookup_value(value):
    """lookup 키로 사용하기 위한 정규화 (공백 제거)."""
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", "", str(value).strip())


def _set_accounting_year_month(df, date_column, year_col="회계연도", month_col="회계월"):
    """날짜 컬럼에서 회계연도/회계월(Int64) 컬럼 생성."""
    if date_column in df.columns:
        parsed = pd.to_datetime(df[date_column], format="mixed", errors="coerce")
        df[year_col] = parsed.dt.year.astype("Int64")
        df[month_col] = parsed.dt.month.astype("Int64")
    else:
        df[year_col] = pd.NA
        df[month_col] = pd.NA
    return df


def _load_excel_sheets(file, clean=True):
    """엑셀 파일에서 모든 시트를 dict로 로드. clean=True 면 컬럼 strip + 빈 row/col 제거."""
    file.seek(0)
    sheets = pd.read_excel(file, sheet_name=None)
    if not clean:
        return sheets

    processed = {}
    for sheet_name, df in sheets.items():
        df = _strip_columns(df)
        df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
        processed[sheet_name] = df
    return processed


# ----- 상품ID 기반 머지/할당 -----

def _merge_by_product_id(target_df, source_df, value_columns):
    """상품ID 정규화 키로 source_df의 value_columns 를 target_df에 left merge."""
    target = target_df.copy()
    target["_상품ID_lookup"] = target["상품ID"].astype(str).str.strip()

    source = source_df.copy()
    source["_상품ID_lookup"] = source["상품ID"].astype(str).str.strip()
    source = source[["_상품ID_lookup", *value_columns]]

    merged = target.merge(source, on="_상품ID_lookup", how="left")
    return merged.drop(columns=["_상품ID_lookup"])


def _allocate_amount(final_df, target_column, total_amount, row_mask):
    """row_mask 행에 total_amount 를 균등 배부 (잔여는 첫 행에 가산)."""
    if total_amount == 0:
        return
    allocation_count = int(row_mask.sum())
    if allocation_count == 0:
        return

    allocation_value = round(total_amount / allocation_count)
    final_df.loc[row_mask, target_column] = allocation_value

    remainder = total_amount - allocation_value * allocation_count
    if remainder != 0:
        first_index = final_df.index[row_mask][0]
        final_df.loc[first_index, target_column] += remainder


def _normal_inbound_mask(final_df):
    """정상입고==1 행 마스크."""
    return pd.to_numeric(final_df["정상입고"], errors="coerce").fillna(0).eq(1)


# ----- 엑셀 출력 -----

def dataframe_to_excel_bytes(df, sheet_name="Sheet1"):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
    output.seek(0)
    return output.getvalue()


def _safe_excel_sheet_name(name, used_names):
    """엑셀 시트명 규칙(31자, 특수문자 등) 적용 + 중복 회피."""
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
    """Streamlit dataframe 표시용 (object/string 컬럼의 NaN → '')."""
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


# ============================================================
# 3. 전처리 - 기초 DB
# ============================================================

def preprocess_purchase_inquiry(file):
    """매입조회"""
    df = _strip_columns(pd.read_excel(file))

    if "상품ID" not in df.columns and "차량아이디" in df.columns:
        df["상품ID"] = df["차량아이디"]

    if "회계월" not in df.columns and "계산서일자" in df.columns:
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


def preprocess_product_master(file):
    """전체상품조회"""
    df = _strip_columns(pd.read_excel(file))
    return _ensure_product_id(df)


def preprocess_consignment_ledger(file, settlement_month):
    """위탁수불부 → (전체, 기초제외==1, 입고제외==1)"""
    df = _ensure_product_id(_strip_columns(pd.read_excel(file)))

    opening_column = f"{settlement_month}월기초_제외"
    inbound_column = f"{settlement_month}월입고_제외"
    if opening_column not in df.columns:
        raise KeyError(f"위탁수불부 파일에 '{opening_column}' 컬럼이 없습니다.")
    if inbound_column not in df.columns:
        raise KeyError(f"위탁수불부 파일에 '{inbound_column}' 컬럼이 없습니다.")

    opening_df = df[_is_flag_one(df[opening_column])].copy()
    inbound_df = df[_is_flag_one(df[inbound_column])].copy()
    return df, opening_df, inbound_df


def preprocess_sales(file):
    """검사매출 / 정비매출"""
    df = _ensure_product_id(_strip_columns(pd.read_excel(file)))

    if "세부내역" in df.columns:
        df = df[~df["세부내역"].astype(str).str.contains("보증수리", na=False)].copy()
    if "상품ID" in df.columns:
        df = df.drop_duplicates(subset=["상품ID"]).copy()
    return df


def preprocess_opening_inventory(file, base_df):
    """기초재고 → (전체, 전월 기말여부==1)"""
    df = _ensure_product_id(_strip_columns(pd.read_excel(file)))

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
    """매입조회에서 기초재고(선매입==1) 상품ID 제외."""
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

    excluded_ids = set(
        df_inventory_all.loc[df_inventory_all["선매입 여부"] == 1, "상품ID"]
        .dropna().astype(str).str.strip()
    )
    purchase_ids = df_purchase["상품ID"].fillna("").astype(str).str.strip()
    return df_purchase[~purchase_ids.isin(excluded_ids)].copy()


# ============================================================
# 4. 차량 상세 / lookup 빌더
# ============================================================

def _build_detail_frame(df, date_column):
    """매입조회/기초재고/전체상품조회/위탁수불부 → 상세 컬럼 통일."""
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


def _build_sales_vehicle_detail_frame(df):
    """검사/정비매출용 - 신번호/구번호만 채우는 단순 상세."""
    columns = ["상품ID", *DETAIL_COLUMNS]
    if df is None or df.empty or "상품ID" not in df.columns:
        return pd.DataFrame(columns=columns)

    temp = _strip_columns(df)
    temp["상품ID"] = temp["상품ID"].astype(str).str.strip()
    temp = temp[temp["상품ID"] != ""].copy()
    if temp.empty:
        return pd.DataFrame(columns=columns)

    vehicle_numbers = _get_first_existing_column(temp, ["차량번호", "신번호"])
    detail = pd.DataFrame({"상품ID": temp["상품ID"]})
    detail["신번호"] = vehicle_numbers
    detail["구번호"] = vehicle_numbers
    for column in DETAIL_COLUMNS:
        if column not in ("신번호", "구번호"):
            detail[column] = ""

    return detail.drop_duplicates(subset=["상품ID"], keep="first").reset_index(drop=True)


def _append_vehicle_details(merged, dfs):
    """출처별 차량 상세를 머지하고 우선순위에 따라 단일 컬럼으로 결정."""
    sources = {
        "매입": _build_detail_frame(dfs.get("매입조회"), "계산서일자"),
        "기초": _build_detail_frame(dfs.get("기초재고"), "계산서일자"),
        "전체": _build_detail_frame(dfs.get("전체상품조회"), "매입세금계산서일자"),
        "위탁수불": _build_detail_frame(dfs.get("위탁수불부_전체"), None),
        "검사": _build_sales_vehicle_detail_frame(dfs.get("검사매출")),
        "정비": _build_sales_vehicle_detail_frame(dfs.get("정비매출")),
    }

    helper_columns = []
    for prefix, detail_df in sources.items():
        renamed = detail_df.rename(
            columns={column: f"{prefix}_{column}" for column in DETAIL_COLUMNS}
        )
        merged = merged.merge(renamed, on="상품ID", how="left")
        helper_columns.extend(f"{prefix}_{column}" for column in DETAIL_COLUMNS)

    use_inventory = merged["_출처"].eq("기초재고")
    use_inspection = merged["_출처"].eq("검사매출")
    use_maintenance = merged["_출처"].eq("정비매출")
    use_product_master = merged["_출처"].eq("전체상품조회")
    use_consignment = merged["_출처"].isin(["위탁수불부_기초", "위탁수불부_입고"])

    for column in DETAIL_COLUMNS:
        purchase_v = merged[f"매입_{column}"].replace("", pd.NA)
        inventory_v = merged[f"기초_{column}"].replace("", pd.NA)
        product_master_v = merged[f"전체_{column}"].replace("", pd.NA)
        consignment_v = merged[f"위탁수불_{column}"].replace("", pd.NA)
        inspection_v = merged[f"검사_{column}"].fillna("")
        maintenance_v = merged[f"정비_{column}"].fillna("")

        default_v = (
            purchase_v.combine_first(inventory_v)
                      .combine_first(product_master_v)
                      .combine_first(consignment_v)
        )
        inventory_result = (
            inventory_v.combine_first(consignment_v)
                       .combine_first(product_master_v)
                       .combine_first(purchase_v)
        )
        product_master_result = (
            product_master_v.combine_first(purchase_v)
                            .combine_first(consignment_v)
                            .combine_first(inventory_v)
        )
        consignment_result = (
            consignment_v.combine_first(product_master_v)
                         .combine_first(purchase_v)
                         .combine_first(inventory_v)
        )

        result = default_v
        result = result.where(~use_product_master, product_master_result)
        result = result.where(~use_consignment, consignment_result)
        result = result.where(~use_inventory, inventory_result)
        result = result.where(~use_inspection, inspection_v)
        result = result.where(~use_maintenance, maintenance_v)
        merged[column] = result.fillna("")

    return merged.drop(columns=helper_columns)


def _build_detail_lookup(detail_df):
    """차량번호(신/구) → 상품ID, 상품ID → 분류1 lookup."""
    empty_lookup = {
        "primary_vehicle_rows": [],
        "secondary_vehicle_rows": [],
        "product_type_by_id": {},
    }
    if detail_df is None or detail_df.empty:
        return empty_lookup

    detail = _strip_columns(detail_df)
    if any(column not in detail.columns for column in ("신번호", "상품ID")):
        return empty_lookup

    detail = detail.copy()
    detail["_신번호_lookup"] = detail["신번호"].apply(_normalize_lookup_value)
    detail["_구번호_lookup"] = (
        detail["구번호"].apply(_normalize_lookup_value)
        if "구번호" in detail.columns else ""
    )
    detail["_상품ID_lookup"] = detail["상품ID"].apply(_normalize_lookup_value)

    primary = (
        detail.loc[detail["_신번호_lookup"] != "", ["_신번호_lookup", "상품ID"]]
              .dropna(subset=["상품ID"]).values.tolist()
    )
    secondary = (
        detail.loc[detail["_구번호_lookup"] != "", ["_구번호_lookup", "상품ID"]]
              .dropna(subset=["상품ID"]).values.tolist()
    )

    product_type_by_id = {}
    if "분류1" in detail.columns:
        type_lookup = (
            detail.loc[detail["_상품ID_lookup"] != "", ["_상품ID_lookup", "분류1"]]
                  .drop_duplicates(subset=["_상품ID_lookup"], keep="first")
        )
        product_type_by_id = dict(zip(type_lookup["_상품ID_lookup"], type_lookup["분류1"]))

    return {
        "primary_vehicle_rows": primary,
        "secondary_vehicle_rows": secondary,
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


def _build_product_ledger_lookup(product_ledger_df):
    """상품원장 차량번호 → 상품ID."""
    if (
        product_ledger_df is None
        or product_ledger_df.empty
        or "차량번호" not in product_ledger_df.columns
        or "상품ID" not in product_ledger_df.columns
    ):
        return {}

    ledger = _strip_columns(product_ledger_df).copy()
    ledger["_차량번호_lookup"] = ledger["차량번호"].apply(_normalize_lookup_value)
    ledger = ledger[ledger["_차량번호_lookup"] != ""].copy()
    if ledger.empty:
        return {}

    ledger = ledger.drop_duplicates(subset=["_차량번호_lookup"], keep="last")
    return dict(zip(ledger["_차량번호_lookup"], ledger["상품ID"]))


def _build_product_id_lookup_by_column(detail_df, lookup_column):
    """detail_df 의 lookup_column → 상품ID."""
    if (
        detail_df is None
        or detail_df.empty
        or lookup_column not in detail_df.columns
        or "상품ID" not in detail_df.columns
    ):
        return {}

    detail = _strip_columns(detail_df).copy()
    detail["_lookup_key"] = detail[lookup_column].apply(_normalize_lookup_value)
    detail = detail[detail["_lookup_key"] != ""].copy()
    if detail.empty:
        return {}

    detail = detail.drop_duplicates(subset=["_lookup_key"], keep="last")
    return dict(zip(detail["_lookup_key"], detail["상품ID"]))


# ============================================================
# 5. 상품ID 모음
# ============================================================

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

    # 입고/기초 플래그
    for column in ("기초재고", "정상입고", "타처입고"):
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

    # 매출구분 / 당사/타사
    merged["구분2"] = ""
    internal_sales = merged[["기초재고", "정상입고", "타처입고"]].eq(1).any(axis=1)
    merged.loc[internal_sales, "구분2"] = "사내매출"
    merged.loc[
        merged["_출처"].isin(["위탁수불부_기초", "위탁수불부_입고"]), "구분2"
    ] = "위탁매출"
    merged.loc[merged["_출처"].eq("검사매출"), "구분2"] = "검사매출"
    merged.loc[merged["_출처"].eq("정비매출"), "구분2"] = "정비매출"
    merged["구분1"] = merged["구분2"].eq("사내매출").map({True: "당사차량", False: "타사차량"})

    # 차량 상세 + 매입연/월
    merged = _append_vehicle_details(merged, dfs)
    parsed_purchase_date = pd.to_datetime(merged["매입일자"], format="mixed", errors="coerce")
    internal_sales = merged["구분2"].eq("사내매출")
    merged["매입연도"] = parsed_purchase_date.dt.year.astype("Int64").where(internal_sales, pd.NA)
    merged["매입월"] = parsed_purchase_date.dt.month.astype("Int64").where(internal_sales, pd.NA)
    merged["회계연도"] = int(settlement_year) if settlement_year is not None else pd.NA
    merged["회계월"] = int(settlement_month) if settlement_month is not None else pd.NA

    merged = merged.drop(columns=["_출처", "_입고구분"])
    merged = merged.rename(columns={"구분1": "당사/타사", "구분2": "매출구분"})
    merged = merged[PRODUCT_ID_COLUMNS]
    return merged.sort_values("상품ID").reset_index(drop=True)


# ============================================================
# 6. 전처리 - 매입원가
# ============================================================

# ----- 상품원장 -----

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


# 원가구분 키워드 → 라벨 (순서 중요: 위에서부터 매칭)
_COST_KEYWORD_RULES = [
    (("매출원가", "재공품", "상품평가충당금"), "결산"),
    (("오류",), "매입수수료"),
    (("초과운행",), "초과운행"),
    (("계약만기 도래분(반납)",), "페이백(반납)"),
    (("계약만기 도래분(미반납)",), "페이백(미반납)"),
    (("폐자원",), "폐자원공제"),
    (("취득세", "취등록세"), "취득세"),
    (("선매입",), "상품매입액"),
    (
        (
            "피알앤디컴퍼니", "경매장", "인품", "엔카", "중개", "알선", "매입",
            "소개수수료", "헤이딜러", "매입수수료", "매입 수수료",
            "낙찰수수료", "낙찰 수수료",
        ),
        "매입수수료",
    ),
    (("(상품->건설중인자산)", "상품->자산"), "자산출고"),
    (("상품전환",), "타처입고"),
]


def classify_cost(row):
    text = str(row["적요"]) if pd.notna(row["적요"]) else ""
    for keywords, label in _COST_KEYWORD_RULES:
        if any(keyword in text for keyword in keywords):
            return label

    reference = row["참고"]
    if pd.notna(reference) and reference not in ("", 0):
        return "상품매입액"
    return ""


def extract_car_number(row):
    text = str(row["적요"]) if pd.notna(row["적요"]) else ""
    cost_type = row["원가구분"]

    if cost_type == "결산":
        return "결산"
    if text.endswith("지게차"):
        return "지게차"
    if cost_type in ("페이백(반납)", "페이백(미반납)", "폐자원공제"):
        return "확인필요"

    match = re.search(r"\d{2,3}[^\d]\d{4}", text)
    return match.group() if match else ""


def _append_product_ledger_purchase_columns(df, detail_df, inventory_all_df=None):
    """상품원장에 상품ID, 매입유형 컬럼 부여."""
    detail_lookup_df = detail_df
    if detail_lookup_df is not None and not detail_lookup_df.empty:
        detail_lookup_df = _strip_columns(detail_lookup_df)
        if "당사/타사" in detail_lookup_df.columns:
            detail_lookup_df = detail_lookup_df[
                detail_lookup_df["당사/타사"].astype(str).str.strip().eq("당사차량")
            ].copy()

    detail_lookup = _build_detail_lookup(detail_lookup_df)
    inventory_detail_df = _build_detail_frame(inventory_all_df, "위탁등록일자")
    inventory_lookup = _build_detail_lookup(inventory_detail_df)

    product_id_cache = {}
    product_ids = []
    for _, row in df.iterrows():
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
    df["매입유형"] = [
        product_type_by_id.get(_normalize_lookup_value(pid), "")
        for pid in df["상품ID"]
    ]
    return df


def preprocess_product_ledger(
    file,
    detail_df=None,
    inventory_all_df=None,
    settlement_year=None,
    settlement_month=None,
):
    """상품원장"""
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
          .transform("count") > 1
    )
    df["상태"] = np.where(canceled, "취소", "")
    df.loc[df["상태"].eq("") & df["원가구분"].eq("결산"), "상태"] = "결산"
    df.drop(columns=["abs_v", "seq"], inplace=True)
    df["금액"] = df["차변"] - df["대변"]

    if "잔액" in df.columns and "작성일자" in df.columns:
        columns_to_remove = df.loc[:, "잔액":"작성일자"].columns
        df.drop(columns=columns_to_remove, inplace=True)

    return _append_product_ledger_purchase_columns(df, detail_df, inventory_all_df)


# ----- 폐자원 -----

def preprocess_waste_resource_file(file, product_ledger_df=None, detail_df=None):
    sheets = _load_excel_sheets(file)
    product_id_by_car_number = _build_product_ledger_lookup(product_ledger_df)
    product_id_by_chassis_number = _build_product_id_lookup_by_column(detail_df, "차대번호")

    processed_sheets = {}
    for sheet_name, df in sheets.items():
        if "구분" in df.columns:
            df = df[df["구분"].astype(str).str.strip().isin(["영수증", "계산서"])].copy()

        # 차량번호 → 상품ID, 지게차는 차대번호로 대체
        if "차량번호" in df.columns:
            df["상품ID"] = df["차량번호"].apply(
                lambda v: product_id_by_car_number.get(_normalize_lookup_value(v), "")
            )
            if "차대번호" in df.columns:
                forklift_rows = df["차량번호"].astype(str).str.strip().eq("지게차")
                df.loc[forklift_rows, "상품ID"] = df.loc[forklift_rows, "차대번호"].apply(
                    lambda v: product_id_by_chassis_number.get(_normalize_lookup_value(v), "")
                )
        else:
            df["상품ID"] = ""

        # 제외대상 (세액공제액 < 0)
        tax_credit_column = next(
            (c for c in WASTE_RESOURCE_AMOUNT_COLUMNS if c in df.columns), None
        )
        if tax_credit_column is not None:
            tax_credit = pd.to_numeric(df[tax_credit_column], errors="coerce").fillna(0)
            df["제외대상"] = np.where(tax_credit < 0, 1, 0)
        else:
            df["제외대상"] = 0

        df = _set_accounting_year_month(df, "매입일자")

        # 컬럼 순서: 첫 컬럼 다음에 [제외대상, 회계연도, 회계월] 삽입
        ordered = [c for c in ("제외대상", "회계연도", "회계월") if c in df.columns]
        rest = [c for c in df.columns if c not in ordered]
        df = df[rest[:1] + ordered + rest[1:]]

        processed_sheets[sheet_name] = df

    return processed_sheets


# ----- 페이백 -----

def _parse_year_month(value):
    if pd.isna(value):
        return pd.NA, pd.NA
    text = str(value).strip()
    match = re.search(r"(\d{4})\D?(\d{1,2})", text)
    if not match:
        return pd.NA, pd.NA
    return int(match.group(1)), int(match.group(2))


def preprocess_payback_file(file, detail_df=None, settlement_year=None, settlement_month=None):
    sheets = _load_excel_sheets(file)
    detail_lookup = _build_detail_lookup(detail_df)

    processed_sheets = {}
    for sheet_name, df in sheets.items():
        # 결산연/월 필터
        if "연도월" in df.columns:
            parsed_period = df["연도월"].apply(_parse_year_month)
            df["연도"] = parsed_period.apply(lambda v: v[0]).astype("Int64")
            df["월"] = parsed_period.apply(lambda v: v[1]).astype("Int64")
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
                normalized_car_number = _normalize_lookup_value(car_number)

                if len(normalized_car_number) == 12:
                    product_id = normalized_car_number
                elif car_number == "지게차":
                    product_id = original_product_ids.loc[index]
                else:
                    product_id = _lookup_product_id_by_car_number(car_number, detail_lookup)
                product_ids.append(product_id)
            df["상품ID"] = product_ids
        elif "상품ID" not in df.columns:
            df["상품ID"] = ""

        processed_sheets[sheet_name] = df

    return processed_sheets


# ----- 일반 원가 파일 -----

def preprocess_cost_file(file):
    """기타 원가 시트 (특별 처리 없음)."""
    return _load_excel_sheets(file)


# ============================================================
# 7. 전처리 - 제조원가
# ============================================================

def _build_vehicle_sales_count_lookup(detail_df):
    """매출구분 × {신번호, 구번호} → 차량별 카운트."""
    lookup = {
        sales_type: {"신번호": {}, "구번호": {}}
        for sales_type in MATERIAL_SALES_COLUMNS
    }
    if detail_df is None or detail_df.empty:
        return lookup

    detail = _strip_columns(detail_df)
    required_columns = ["매출구분", "신번호", "구번호"]
    if any(column not in detail.columns for column in required_columns):
        return lookup

    detail = detail.copy()
    detail["_신번호_lookup"] = detail["신번호"].apply(_normalize_lookup_value)
    detail["_구번호_lookup"] = detail["구번호"].apply(_normalize_lookup_value)
    detail["매출구분"] = detail["매출구분"].astype(str).str.strip()

    for sales_type in MATERIAL_SALES_COLUMNS:
        sales_rows = detail[detail["매출구분"].eq(sales_type)]
        new_counts = sales_rows.loc[
            sales_rows["_신번호_lookup"] != "", "_신번호_lookup"
        ].value_counts()
        old_counts = sales_rows.loc[
            sales_rows["_구번호_lookup"] != "", "_구번호_lookup"
        ].value_counts()
        lookup[sales_type]["신번호"] = new_counts.to_dict()
        lookup[sales_type]["구번호"] = old_counts.to_dict()

    return lookup


def _get_row_sales_count(row, sales_type, sales_count_lookup):
    vehicle_key = _normalize_lookup_value(row["차량번호"] if "차량번호" in row.index else "")
    old_vehicle_key = _normalize_lookup_value(row["구차량번호"] if "구차량번호" in row.index else "")
    return (
        sales_count_lookup[sales_type]["신번호"].get(vehicle_key, 0)
        + sales_count_lookup[sales_type]["구번호"].get(old_vehicle_key, 0)
    )


def _build_direct_expense_sales_count_lookup(detail_df):
    """직접경비 전용: 매출구분 × {신번호, 구번호, 상품ID} → 카운트.

    직접경비는 차량번호 컬럼 하나만 있으므로, 차량번호를 신번호/구번호 양쪽과
    매칭하고 상품아이디를 상품ID 와도 매칭하기 위한 lookup.
    """
    lookup = {
        sales_type: {"신번호": {}, "구번호": {}, "상품ID": {}}
        for sales_type in MATERIAL_SALES_COLUMNS
    }
    if detail_df is None or detail_df.empty:
        return lookup

    detail = _strip_columns(detail_df)
    if "매출구분" not in detail.columns:
        return lookup

    detail = detail.copy()
    detail["매출구분"] = detail["매출구분"].astype(str).str.strip()
    if "신번호" in detail.columns:
        detail["_신번호_lookup"] = detail["신번호"].apply(_normalize_lookup_value)
    else:
        detail["_신번호_lookup"] = ""
    if "구번호" in detail.columns:
        detail["_구번호_lookup"] = detail["구번호"].apply(_normalize_lookup_value)
    else:
        detail["_구번호_lookup"] = ""
    if "상품ID" in detail.columns:
        detail["_상품ID_lookup"] = detail["상품ID"].apply(_normalize_lookup_value)
    else:
        detail["_상품ID_lookup"] = ""

    for sales_type in MATERIAL_SALES_COLUMNS:
        sales_rows = detail[detail["매출구분"].eq(sales_type)]
        lookup[sales_type]["신번호"] = (
            sales_rows.loc[sales_rows["_신번호_lookup"] != "", "_신번호_lookup"]
            .value_counts().to_dict()
        )
        lookup[sales_type]["구번호"] = (
            sales_rows.loc[sales_rows["_구번호_lookup"] != "", "_구번호_lookup"]
            .value_counts().to_dict()
        )
        lookup[sales_type]["상품ID"] = (
            sales_rows.loc[sales_rows["_상품ID_lookup"] != "", "_상품ID_lookup"]
            .value_counts().to_dict()
        )

    return lookup


def _get_direct_expense_sales_count(
    sales_type, lookup, car_number, source_product_id,
):
    """직접경비 행의 매출구분 카운트.

    = (차량번호 ↔ 신번호 카운트)
    + (차량번호 ↔ 구번호 카운트)
    + (상품아이디 ↔ 상품ID 카운트)
    """
    car_key = _normalize_lookup_value(car_number)
    product_key = _normalize_lookup_value(source_product_id)
    counts = lookup[sales_type]
    return (
        counts["신번호"].get(car_key, 0)
        + counts["구번호"].get(car_key, 0)
        + counts["상품ID"].get(product_key, 0)
    )


def _build_segment_to_product_id_lookups(detail_df):
    """detail_df (product_id_df) 에서 다음 두 dict 반환.

    new_lookup: {매출구분}_{신번호} → 상품ID
    old_lookup: {매출구분}_{구번호} → 상품ID
    """
    new_lookup = {}
    old_lookup = {}
    if detail_df is None or detail_df.empty:
        return new_lookup, old_lookup

    detail = _strip_columns(detail_df)
    if "매출구분" not in detail.columns or "상품ID" not in detail.columns:
        return new_lookup, old_lookup

    detail = detail.copy()
    detail["_매출구분"] = detail["매출구분"].astype(str).str.strip()
    detail["_상품ID"] = detail["상품ID"].astype(str).str.strip()
    detail = detail[detail["_매출구분"].ne("") & detail["_상품ID"].ne("")]

    if "신번호" in detail.columns:
        for _, row in detail.iterrows():
            new_no = _normalize_lookup_value(row["신번호"])
            if new_no:
                key = f"{row['_매출구분']}_{new_no}"
                # 먼저 본 값을 유지 (중복 시 첫 매칭 우선)
                new_lookup.setdefault(key, row["_상품ID"])

    if "구번호" in detail.columns:
        for _, row in detail.iterrows():
            old_no = _normalize_lookup_value(row["구번호"])
            if old_no:
                key = f"{row['_매출구분']}_{old_no}"
                old_lookup.setdefault(key, row["_상품ID"])

    return new_lookup, old_lookup


def _build_sales_type_product_id_lookup(detail_df):
    """detail_df 에서 {매출구분}_{상품ID} → 상품ID lookup 반환."""
    lookup = {}
    if detail_df is None or detail_df.empty:
        return lookup

    detail = _strip_columns(detail_df)
    if "매출구분" not in detail.columns or "상품ID" not in detail.columns:
        return lookup

    detail = detail.copy()
    detail["_매출구분"] = detail["매출구분"].astype(str).str.strip()
    detail["_상품ID"] = detail["상품ID"].apply(_normalize_lookup_value)
    detail = detail[detail["_매출구분"].ne("") & detail["_상품ID"].ne("")]

    for _, row in detail.iterrows():
        key = f"{row['_매출구분']}_{row['_상품ID']}"
        lookup.setdefault(key, row["_상품ID"])

    return lookup


def _build_product_id_to_sales_type_lookup(detail_df):
    """detail_df 에서 상품ID → 매출구분 lookup 반환."""
    lookup = {}
    if detail_df is None or detail_df.empty:
        return lookup

    detail = _strip_columns(detail_df)
    if "매출구분" not in detail.columns or "상품ID" not in detail.columns:
        return lookup

    detail = detail.copy()
    detail["_매출구분"] = detail["매출구분"].astype(str).str.strip()
    detail["_상품ID"] = detail["상품ID"].apply(_normalize_lookup_value)
    detail = detail[detail["_매출구분"].ne("") & detail["_상품ID"].ne("")]

    for _, row in detail.iterrows():
        lookup.setdefault(row["_상품ID"], row["_매출구분"])

    return lookup


def preprocess_material_cost_file(file, detail_df=None):
    """재료비"""
    sheets = _load_excel_sheets(file)
    sales_count_lookup = _build_vehicle_sales_count_lookup(detail_df)
    # 재료비 시트의 '구분자' (= 매출구분_차량번호) 로 상품ID 찾기 위한 lookup
    new_no_lookup, old_no_lookup = _build_segment_to_product_id_lookups(detail_df)

    processed_sheets = {}
    for sheet_name, df in sheets.items():
        if "출고부품분류" in df.columns:
            df["원가구분"] = np.where(
                df["출고부품분류"].astype(str).str.strip().eq("원재료비 대체"),
                "재료비", "",
            )
        else:
            df["원가구분"] = ""

        df = _set_accounting_year_month(df, "차량입고일자", year_col="출고년도", month_col="출고월")

        for sales_type in MATERIAL_SALES_COLUMNS:
            df[sales_type] = df.apply(
                lambda row: _get_row_sales_count(row, sales_type, sales_count_lookup),
                axis=1,
            )

        df["매출구분"] = np.select(
            [df["정비매출"].gt(0), df["위탁매출"].gt(0), df["사내매출"].gt(0)],
            ["정비매출", "위탁매출", "사내매출"],
            default="",
        )

        vehicle_numbers = (
            df["차량번호"].apply(lambda v: "" if pd.isna(v) else str(v).strip())
            if "차량번호" in df.columns
            else pd.Series([""] * len(df), index=df.index)
        )
        df["구분자"] = np.where(
            df["매출구분"].astype(str).str.strip().ne("") & vehicle_numbers.ne(""),
            df["매출구분"].astype(str).str.strip() + "_" + vehicle_numbers,
            "",
        )

        # 상품ID 매핑: 구분자 (매출구분_신번호) 로 lookup, 없으면 (매출구분_구번호) 로 재시도
        product_id_cache = {}
        product_ids = []
        for segment_key in df["구분자"]:
            key = str(segment_key).strip() if pd.notna(segment_key) else ""
            if not key:
                product_ids.append("")
                continue
            if key not in product_id_cache:
                pid = new_no_lookup.get(key, "")
                if not pid:
                    pid = old_no_lookup.get(key, "")
                product_id_cache[key] = pid
            product_ids.append(product_id_cache[key])
        df["상품ID"] = product_ids

        processed_sheets[sheet_name] = df

    return processed_sheets


# 배부대상 매핑 (적요 키워드 → 배부대상)
# 주의: 위에서부터 먼저 매칭됨(np.select). '정비판금파트' 는 '판금파트'/'정비파트' 를
#       부분 문자열로 포함하므로 반드시 그 두 규칙보다 위에 둬야 정비로 매칭된다.
_ALLOCATION_TARGET_RULES = [
    ("RQI", "RQI"),
    ("임차", "임차"),
    ("반납운영팀", "선물"),
    ("공정지원팀", "전체"),
    ("정비판금파트", "정비"),
    ("도장파트", "도장"),
    ("판금파트", "판금"),
    ("정비파트", "정비"),
]


def preprocess_combined_manufacturing_cost_files(
    files, cost_type, settlement_year=None, settlement_month=None,
):
    """노무비 / 부문별경비"""
    cost_frames = []
    for file in files:
        for df in _load_excel_sheets(file).values():
            if not df.empty:
                cost_frames.append(df)

    if not cost_frames:
        return pd.DataFrame()

    df = pd.concat(cost_frames, ignore_index=True)

    if "회계일자" in df.columns:
        accounting_date_text = df["회계일자"].astype(str).str.strip()
        df = df[~accounting_date_text.isin(["월계", "누계"])].copy()
    if "작성사원명" in df.columns:
        df = df[df["작성사원명"].astype(str).str.strip().ne("김겸윤")].copy()

    df["원가구분"] = cost_type
    df = _set_accounting_year_month(df, "회계일자")

    if settlement_year is not None and settlement_month is not None:
        df = df[
            (df["회계연도"] == int(settlement_year))
            & (df["회계월"] == int(settlement_month))
        ].copy()

    summary_text = (
        df["적요"].astype(str)
        if "적요" in df.columns
        else pd.Series([""] * len(df), index=df.index)
    )
    conditions = [summary_text.str.contains(keyword, na=False) for keyword, _ in _ALLOCATION_TARGET_RULES]
    choices = [label for _, label in _ALLOCATION_TARGET_RULES]
    df["배부대상"] = np.select(conditions, choices, default="기타")

    return df.reset_index(drop=True)


def preprocess_direct_expense_file(file, detail_df=None):
    """직접경비"""
    sheets = _load_excel_sheets(file)
    sales_count_lookup = _build_direct_expense_sales_count_lookup(detail_df)
    new_no_lookup, old_no_lookup = _build_segment_to_product_id_lookups(detail_df)
    product_id_lookup = _build_sales_type_product_id_lookup(detail_df)
    sales_type_by_product_id = _build_product_id_to_sales_type_lookup(detail_df)

    processed_sheets = {}
    for sheet_name, df in sheets.items():
        # 원본 상품아이디/차량아이디 컬럼은 그대로 두고, 최종 매칭용 상품ID는 뒤에서 새로 만든다.
        source_product_id_column = next(
            (column for column in ["상품아이디", "차량아이디", "상품ID"] if column in df.columns),
            None,
        )
        df = _set_accounting_year_month(df, "매입일")

        vehicle_numbers = (
            df["차량번호"].apply(lambda v: "" if pd.isna(v) else str(v).strip())
            if "차량번호" in df.columns
            else pd.Series([""] * len(df), index=df.index)
        )
        source_product_ids = (
            df[source_product_id_column].apply(lambda v: "" if pd.isna(v) else str(v).strip())
            if source_product_id_column is not None
            else pd.Series([""] * len(df), index=df.index)
        )

        # 매출구분 카운트: 차량번호 ↔ 신번호/구번호 + 상품아이디 ↔ 상품ID 3개 합산
        for sales_type in MATERIAL_SALES_COLUMNS:
            df[sales_type] = [
                _get_direct_expense_sales_count(
                    sales_type, sales_count_lookup, car_number, product_id,
                )
                for car_number, product_id in zip(vehicle_numbers, source_product_ids)
            ]

        df["매출구분"] = np.select(
            [df["정비매출"].gt(0), df["위탁매출"].gt(0), df["사내매출"].gt(0)],
            ["정비매출", "위탁매출", "사내매출"],
            default="",
        )
        fallback_sales_type = source_product_ids.apply(
            lambda value: sales_type_by_product_id.get(_normalize_lookup_value(value), "")
        )
        df["매출구분"] = df["매출구분"].where(
            df["매출구분"].astype(str).str.strip().ne(""),
            fallback_sales_type,
        )

        sales_type_series = df["매출구분"].astype(str).str.strip()

        df["구분자1"] = np.where(
            sales_type_series.ne("") & vehicle_numbers.ne(""),
            sales_type_series + "_" + vehicle_numbers,
            "",
        )
        df["구분자2"] = np.where(
            sales_type_series.ne("") & source_product_ids.ne(""),
            sales_type_series + "_" + source_product_ids.apply(_normalize_lookup_value),
            "",
        )

        product_id_cache = {}
        product_ids = []
        for segment_key_1, segment_key_2 in zip(df["구분자1"], df["구분자2"]):
            key_1 = str(segment_key_1).strip() if pd.notna(segment_key_1) else ""
            key_2 = str(segment_key_2).strip() if pd.notna(segment_key_2) else ""
            cache_key = (key_1, key_2)
            if cache_key not in product_id_cache:
                product_id = ""
                if key_1:
                    product_id = new_no_lookup.get(key_1, "")
                    if not product_id:
                        product_id = old_no_lookup.get(key_1, "")
                if not product_id and key_2:
                    product_id = product_id_lookup.get(key_2, "")
                product_id_cache[cache_key] = product_id
            product_ids.append(product_id_cache[cache_key])
        df["상품ID"] = product_ids

        if "구분자2" in df.columns:
            ordered_columns = [column for column in df.columns if column != "상품ID"]
            insert_at = ordered_columns.index("구분자2") + 1
            ordered_columns = (
                ordered_columns[:insert_at] + ["상품ID"] + ordered_columns[insert_at:]
            )
            df = df[ordered_columns]

        processed_sheets[sheet_name] = df
    return processed_sheets