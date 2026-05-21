"""손익분석 - 최종 원가 빌더 모듈

상품ID 모음 + 매입원가 시트들을 모아 최종 원가 DataFrame 을 생성한다.
핵심 로직:
    - 상품원장에서 상품매입액/취득세/매입수수료 집계
    - 폐자원공제, 페이백(반납) 시트 집계
    - 차액배부: 상품원장 합계와 final_df 합계의 차이를 당사차량 정상입고 행에 균등 배부
    - 페이백(미반납), 초과운행: 선물 정상입고 행에 균등 배부
    - 전월/합계 컬럼: 기초재고에서 전월값을 가져와 합계 산출
    - 타사차량 행은 모든 금액 0 처리
"""

import re

import numpy as np
import pandas as pd

from cost_summary_preprocess import (
    # 상수
    PURCHASE_AMOUNT_COLUMNS,
    WASTE_RESOURCE_COLUMN,
    PAYBACK_RETURN_COLUMN,
    PAYBACK_UNRETURNED_COLUMN,
    EXCESS_DRIVING_COLUMN,
    DIFFERENCE_ALLOCATION_COLUMN,
    TOTAL_PURCHASE_COST_COLUMN,
    FINAL_COST_AMOUNT_COLUMNS,
    FINAL_COST_MONTHLY_COLUMNS,
    DIFFERENCE_SOURCE_COST_COLUMNS,
    PRODUCT_LEDGER_TOTAL_COST_COLUMNS,
    WASTE_RESOURCE_AMOUNT_COLUMNS,
    PAYBACK_RETURN_AMOUNT_COLUMNS,
    MATERIAL_COST_AMOUNT_COLUMNS,
    # 헬퍼
    _strip_columns,
    _is_flag_one,
    _merge_by_product_id,
    _allocate_amount,
    _normal_inbound_mask,
)


# ============================================================
# 시트 집계 헬퍼 (폐자원, 페이백 공통)
# ============================================================

def _aggregate_sheet_amounts_by_product_id(
    purchase_cost_sheet_dfs,
    sheet_prefix,
    amount_column_candidates,
    output_column,
    apply_exclusion=False,
):
    """sheet_prefix 로 시작하는 시트들에서 amount 컬럼을 모아 상품ID별 합계 (음수 부호)."""
    frames = []
    for sheet_name, df in purchase_cost_sheet_dfs.items():
        if not str(sheet_name).startswith(sheet_prefix):
            continue
        if df is None or df.empty or "상품ID" not in df.columns:
            continue

        amount_column = next(
            (c for c in amount_column_candidates if c in df.columns), None
        )
        if amount_column is None:
            continue

        selected_columns = ["상품ID", amount_column]
        if apply_exclusion and "제외대상" in df.columns:
            selected_columns.append("제외대상")

        temp = df[selected_columns].copy()
        if apply_exclusion and "제외대상" in temp.columns:
            temp = temp[~_is_flag_one(temp["제외대상"])].copy()
            temp = temp.drop(columns=["제외대상"])

        temp = temp.rename(columns={amount_column: output_column})
        frames.append(temp)

    if not frames:
        return None

    combined = pd.concat(frames, ignore_index=True)
    combined["상품ID"] = combined["상품ID"].astype(str).str.strip()
    combined[output_column] = -pd.to_numeric(
        combined[output_column], errors="coerce"
    ).fillna(0).abs()
    combined = combined[combined["상품ID"].ne("")].copy()

    if combined.empty:
        return None

    return combined.groupby("상품ID", as_index=False)[output_column].sum()


def _apply_aggregated_amount(final_df, aggregated_df, output_column):
    """집계 결과를 상품ID 기준으로 final_df 의 output_column 에 덮어쓰기."""
    if aggregated_df is None or aggregated_df.empty:
        return final_df
    final_df = final_df.drop(columns=[output_column])
    return _merge_by_product_id(final_df, aggregated_df, [output_column])


# ============================================================
# 상품원장 집계
# ============================================================

def _extract_product_ledger_aggregates(purchase_cost_sheet_dfs):
    """상품원장 시트에서 (cost_totals, transfer_in_map, purchase_amount_df) 추출."""
    required_columns = ["상품ID", "원가구분", "금액"]
    frames = []
    for sheet_name, df in purchase_cost_sheet_dfs.items():
        if not str(sheet_name).startswith("상품원장"):
            continue
        if df is None or df.empty:
            continue
        if any(column not in df.columns for column in required_columns):
            continue
        frames.append(df[required_columns].copy())

    cost_totals = {column: 0 for column in PRODUCT_LEDGER_TOTAL_COST_COLUMNS}
    transfer_in_map = {}
    purchase_amount_df = None

    if not frames:
        return cost_totals, transfer_in_map, purchase_amount_df

    ledger = pd.concat(frames, ignore_index=True)
    ledger["상품ID"] = ledger["상품ID"].astype(str).str.strip()
    ledger["원가구분"] = ledger["원가구분"].astype(str).str.strip()
    ledger["금액"] = pd.to_numeric(ledger["금액"], errors="coerce").fillna(0)

    cost_totals.update(
        ledger[ledger["원가구분"].isin(PRODUCT_LEDGER_TOTAL_COST_COLUMNS)]
        .groupby("원가구분")["금액"].sum().to_dict()
    )

    transfer_in_df = ledger[
        ledger["원가구분"].eq("타처입고") & ledger["상품ID"].ne("")
    ]
    if not transfer_in_df.empty:
        transfer_in_map = transfer_in_df.groupby("상품ID")["금액"].sum().to_dict()

    purchase_rows = ledger[
        ledger["원가구분"].isin(PURCHASE_AMOUNT_COLUMNS) & ledger["상품ID"].ne("")
    ]
    if not purchase_rows.empty:
        purchase_amount_df = (
            purchase_rows.pivot_table(
                index="상품ID", columns="원가구분", values="금액",
                aggfunc="sum", fill_value=0,
            ).reset_index().rename_axis(None, axis=1)
        )
        for column in PURCHASE_AMOUNT_COLUMNS:
            if column not in purchase_amount_df.columns:
                purchase_amount_df[column] = 0

    return cost_totals, transfer_in_map, purchase_amount_df


# ============================================================
# 전월/합계 컬럼
# ============================================================

def _append_previous_month_cost_columns(
    final_df, inventory_df, settlement_month=None, transfer_in_amount_map=None,
):
    """전월/합계 컬럼 부여."""
    previous_columns = [f"{column}_전월" for column in FINAL_COST_MONTHLY_COLUMNS]
    for column in previous_columns:
        final_df[column] = 0

    # 기초재고에서 전월 값 가져오기
    if (
        inventory_df is not None
        and not inventory_df.empty
        and "상품ID" in inventory_df.columns
    ):
        inventory = _strip_columns(inventory_df).copy()

        if settlement_month is not None:
            previous_month = 12 if int(settlement_month) == 1 else int(settlement_month) - 1
            previous_flag_column = f"{previous_month}월 기말여부"
            if previous_flag_column in inventory.columns:
                inventory = inventory[_is_flag_one(inventory[previous_flag_column])].copy()
            else:
                inventory = inventory.iloc[0:0].copy()

        inventory["상품ID"] = inventory["상품ID"].astype(str).str.strip()
        inventory = inventory[inventory["상품ID"].ne("")].copy()

        available_previous_columns = [
            column for column in previous_columns if column in inventory.columns
        ]

        if available_previous_columns and not inventory.empty:
            for column in available_previous_columns:
                inventory[column] = pd.to_numeric(
                    inventory[column], errors="coerce"
                ).fillna(0)

            previous_df = (
                inventory.groupby("상품ID", as_index=False)[available_previous_columns].sum()
            )
            final_df = final_df.drop(columns=available_previous_columns)
            final_df = _merge_by_product_id(final_df, previous_df, available_previous_columns)

    # 합계 = 전월 + 당월
    for column in FINAL_COST_MONTHLY_COLUMNS:
        previous_column = f"{column}_전월"
        total_column = f"{column}_합계"
        if column not in final_df.columns:
            final_df[column] = 0
        if previous_column not in final_df.columns:
            final_df[previous_column] = 0
        final_df[column] = pd.to_numeric(final_df[column], errors="coerce").fillna(0)
        final_df[previous_column] = pd.to_numeric(
            final_df[previous_column], errors="coerce"
        ).fillna(0)
        final_df[total_column] = final_df[previous_column] + final_df[column]

    # 타처입고 행의 상품매입액_전월은 상품원장(원가구분=타처입고) 금액으로 덮어쓰기
    if (
        transfer_in_amount_map is not None
        and "타처입고" in final_df.columns
        and "상품매입액_전월" in final_df.columns
    ):
        transfer_in_rows = _is_flag_one(final_df["타처입고"])
        if transfer_in_rows.any():
            lookup_ids = final_df.loc[transfer_in_rows, "상품ID"].astype(str).str.strip()
            mapped_amounts = (
                lookup_ids.map(transfer_in_amount_map)
                .pipe(lambda s: pd.to_numeric(s, errors="coerce"))
                .fillna(0)
            )
            final_df["상품매입액_전월"] = pd.to_numeric(
                final_df["상품매입액_전월"], errors="coerce"
            ).fillna(0).astype(float)
            final_df.loc[transfer_in_rows, "상품매입액_전월"] = mapped_amounts.values
            final_df["상품매입액_합계"] = (
                pd.to_numeric(final_df["상품매입액_전월"], errors="coerce").fillna(0)
                + pd.to_numeric(final_df["상품매입액"], errors="coerce").fillna(0)
            )

    # 타사차량 행의 전월/합계는 0 처리
    if "당사/타사" in final_df.columns:
        own_vehicle_rows = final_df["당사/타사"].astype(str).str.strip().eq("당사차량")
        non_own = ~own_vehicle_rows
        if non_own.any():
            related_columns = [
                f"{column}{suffix}"
                for column in FINAL_COST_MONTHLY_COLUMNS
                for suffix in ("_전월", "_합계")
            ]
            existing = [c for c in related_columns if c in final_df.columns]
            final_df.loc[non_own, existing] = 0

    return final_df


# ============================================================
# 재료비 컬럼 (재료비_직접 / 재료비_배부 / 재료비_합)
# ============================================================

# 재료비 시트로 인식할 라벨 prefix (제조원가 업로드에서 "재료비" 라벨 사용)
MATERIAL_COST_SHEET_PREFIX = "재료비"


def _allocate_amount_proportional(
    final_df, target_column, total_amount, weight_series, row_mask=None,
):
    """row_mask 행에 total_amount 를 weight_series 비율로 배부.

    라운딩 오차는 row_mask 중 weight 가 0 이 아닌 첫 행에 가산하여 sum == total_amount 보장.
    (weight=0 인 빈 행에 잔여가 떨어지지 않도록.)
    weight 합이 0이면 배부하지 않음 (분모 0 회피).
    """
    if total_amount == 0:
        return
    if row_mask is None:
        row_mask = pd.Series(True, index=final_df.index)

    weights = pd.to_numeric(weight_series[row_mask], errors="coerce").fillna(0)
    weights_sum = weights.sum()
    if weights_sum == 0:
        return

    allocations = (weights / weights_sum * total_amount).round()
    # int dtype 컬럼에 float 대입 방지 (pandas 2.x)
    final_df[target_column] = pd.to_numeric(
        final_df[target_column], errors="coerce"
    ).fillna(0).astype(float)
    final_df.loc[row_mask, target_column] = allocations.values

    remainder = total_amount - allocations.sum()
    if remainder != 0:
        # weight 가 0 이 아닌 첫 행에 잔여 가산
        nonzero_indices = final_df.index[row_mask][weights.values != 0]
        if len(nonzero_indices) > 0:
            final_df.loc[nonzero_indices[0], target_column] += remainder
        else:
            # 모든 가중치가 0 인 케이스 (도달 불가지만 안전장치)
            first_index = final_df.index[row_mask][0]
            final_df.loc[first_index, target_column] += remainder


def _find_settlement_month_column(columns, settlement_year, settlement_month):
    """검증시트 컬럼 중 결산연도+결산월(예: '202601') 문자열을 포함하는 컬럼 반환.

    공백은 제거하고 매칭. 예: '잔액 202601', '202601_금액', '2026.01 합계' 등 모두 매칭.
    또한 datetime / Timestamp 객체 컬럼명도 처리.
    월 숫자만 컬럼명으로 들어오는 검증시트(예: 1, '1월')도 처리.
    """
    if settlement_month is None or settlement_year is None:
        return None
    year = int(settlement_year)
    month = int(settlement_month)
    settlement_key = f"{year}{month:02d}"  # 예: '202601'

    # 1) 문자열 컬럼명에서 'YYYYMM' 포함 여부 (공백 제거 후 검사)
    for column in columns:
        column_str = re.sub(r"\s+", "", str(column))
        column_digits = re.sub(r"\D", "", column_str)
        if settlement_key in column_str or settlement_key in column_digits:
            return column

    # 2) datetime / Timestamp 객체 컬럼명 매칭
    for column in columns:
        if hasattr(column, "year") and hasattr(column, "month"):
            if column.month == month and column.year == year:
                return column

    # 3) 월 숫자만 있는 컬럼명 매칭
    month_candidates = {str(month), f"{month:02d}", f"{month}월", f"{month:02d}월"}
    for column in columns:
        column_str = re.sub(r"\s+", "", str(column))
        if column_str in month_candidates:
            return column

    return None


def _get_material_cost_total_from_verification(
    verification_sheets, settlement_year, settlement_month,
):
    """검증시트에서 '계정명' 에 '재료비' 가 포함된 행의 결산월 컬럼 값을 합산해 총 재료비 반환."""
    if not verification_sheets:
        return None

    for sheet_name, df in verification_sheets.items():
        if df is None or df.empty or "계정명" not in df.columns:
            continue

        material_rows = df[
            df["계정명"].astype(str).str.contains("재료비", na=False)
        ]
        if material_rows.empty:
            continue

        month_column = _find_settlement_month_column(
            df.columns, settlement_year, settlement_month,
        )
        if month_column is None:
            continue

        values = pd.to_numeric(material_rows[month_column], errors="coerce").fillna(0)
        return float(values.sum())

    return None


def _aggregate_material_cost_by_product_id(manufacturing_cost_sheet_dfs):
    """재료비 시트들에서 매출구분이 '검사매출'이 아닌 행만 추려
    (상품ID, 매출구분)별 금액 합산.

    final_df 와 머지 시 상품ID 뿐 아니라 매출구분도 일치해야 매칭됨.
    """
    if not manufacturing_cost_sheet_dfs:
        return None

    frames = []
    for sheet_name, df in manufacturing_cost_sheet_dfs.items():
        if not str(sheet_name).startswith(MATERIAL_COST_SHEET_PREFIX):
            continue
        if df is None or df.empty:
            continue
        if "상품ID" not in df.columns or "매출구분" not in df.columns:
            continue

        amount_column = next(
            (c for c in MATERIAL_COST_AMOUNT_COLUMNS if c in df.columns), None
        )
        if amount_column is None:
            continue

        temp = df[["상품ID", "매출구분", amount_column]].copy()
        temp["매출구분"] = temp["매출구분"].astype(str).str.strip()
        temp = temp[~temp["매출구분"].eq("검사매출")]
        temp = temp.rename(columns={amount_column: "재료비_직접"})
        frames.append(temp)

    if not frames:
        return None

    combined = pd.concat(frames, ignore_index=True)
    combined["상품ID"] = combined["상품ID"].astype(str).str.strip()
    combined["재료비_직접"] = pd.to_numeric(
        combined["재료비_직접"], errors="coerce"
    ).fillna(0)
    combined = combined[combined["상품ID"].ne("")].copy()

    if combined.empty:
        return None

    return combined.groupby(
        ["상품ID", "매출구분"], as_index=False
    )["재료비_직접"].sum()


def _append_material_cost_columns(
    final_df,
    manufacturing_cost_sheet_dfs,
    verification_sheets=None,
    settlement_year=None,
    settlement_month=None,
):
    """재료비_직접 / 재료비_배부 / 재료비_합 컬럼을 부여.

    재료비_직접: 재료비 시트에서 (매출구분 != '검사매출') & (상품ID + 매출구분 일치) 행의 금액 합
    재료비_배부: 검증시트의 (원가='재료비', 결산월 컬럼) 값 - 재료비_직접 합 을
                재료비_직접 값 비율로 검사매출 외 행에 분배. 라운딩 차이는 첫 행에 가산.
    재료비_합:   재료비_직접 + 재료비_배부 (총합은 검증시트 값과 동일).
    """
    final_df["재료비_직접"] = 0
    final_df["재료비_배부"] = 0
    final_df["재료비_합"] = 0

    # 1) 재료비_직접 집계 (상품ID + 매출구분 으로 머지)
    aggregated_df = _aggregate_material_cost_by_product_id(manufacturing_cost_sheet_dfs)
    if (
        aggregated_df is not None and not aggregated_df.empty
        and "매출구분" in final_df.columns
    ):
        final_df = final_df.drop(columns=["재료비_직접"])

        # 정규화 키로 (상품ID, 매출구분) 머지
        final_df["_상품ID_lookup"] = final_df["상품ID"].astype(str).str.strip()
        final_df["_매출구분_lookup"] = final_df["매출구분"].astype(str).str.strip()

        source = aggregated_df.copy()
        source["_상품ID_lookup"] = source["상품ID"].astype(str).str.strip()
        source["_매출구분_lookup"] = source["매출구분"].astype(str).str.strip()
        source = source[["_상품ID_lookup", "_매출구분_lookup", "재료비_직접"]]

        final_df = final_df.merge(
            source, on=["_상품ID_lookup", "_매출구분_lookup"], how="left",
        )
        final_df = final_df.drop(columns=["_상품ID_lookup", "_매출구분_lookup"])
        final_df["재료비_직접"] = pd.to_numeric(
            final_df["재료비_직접"], errors="coerce"
        ).fillna(0)

        # 검사매출 행은 안전하게 0 (정의상 검사매출 제외)
        inspection_rows = final_df["매출구분"].astype(str).str.strip().eq("검사매출")
        final_df.loc[inspection_rows, "재료비_직접"] = 0

    # 2) 재료비_배부 (검증시트 기반 비례 배부)
    total_material_cost = _get_material_cost_total_from_verification(
        verification_sheets, settlement_year, settlement_month,
    )
    if total_material_cost is not None and "매출구분" in final_df.columns:
        direct_sum = pd.to_numeric(
            final_df["재료비_직접"], errors="coerce"
        ).fillna(0).sum()
        allocation_total = total_material_cost - direct_sum

        if allocation_total != 0:
            non_inspection_mask = ~final_df["매출구분"].astype(str).str.strip().eq("검사매출")
            _allocate_amount_proportional(
                final_df,
                "재료비_배부",
                allocation_total,
                final_df["재료비_직접"],
                non_inspection_mask,
            )

    # 3) 재료비_합 = 직접 + 배부
    final_df["재료비_합"] = (
        pd.to_numeric(final_df["재료비_직접"], errors="coerce").fillna(0)
        + pd.to_numeric(final_df["재료비_배부"], errors="coerce").fillna(0)
    )
    return final_df


# ============================================================
# 제조경비 컬럼 (제조경비_직접 / 임차 / 전체 / RQI / 정비 / 판금 / 도장)
# ============================================================

# 직접경비 시트 인식 prefix (제조원가 업로드에서 "직접경비" 라벨 사용)
DIRECT_EXPENSE_SHEET_PREFIX = "직접경비"
# 직접경비 시트의 금액 컬럼 후보
DIRECT_EXPENSE_AMOUNT_COLUMNS = ["금액", "공급가액", "공급가", "원가"]

# 부문별경비 시트 인식 prefix (제조원가 업로드에서 "부문별경비" 라벨 사용)
DEPARTMENT_EXPENSE_SHEET_PREFIX = "부문별경비"

# 제조경비 배부 컬럼: (결과 컬럼명, 검증시트 부문/부문별경비 배부대상, 배부 가중치 컬럼)
MANUFACTURING_EXPENSE_ALLOCATION_SPECS = [
    ("제조경비_임차", "임차", "sm_일수"),
    ("제조경비_전체", "전체", "rtc_일수"),
    ("제조경비_RQI",  "RQI",  "공정별_RQI"),
    ("제조경비_정비", "정비", "공정별_정비"),
    ("제조경비_판금", "판금", "공정별_판금"),
    ("제조경비_도장", "도장", "공정별_도장"),
]
MANUFACTURING_EXPENSE_GIFT_COLUMN = "제조경비_선물"
MANUFACTURING_EXPENSE_GIFT_TARGET = "선물"
MANUFACTURING_EXPENSE_GIFT_WEIGHT_COLUMN = "rtc_일수"
MANUFACTURING_EXPENSE_GIFT_FILTER_COLUMN = "분류1"
MANUFACTURING_EXPENSE_GIFT_FILTER_VALUE = "선물"
MANUFACTURING_EXPENSE_EXTRA_ALLOCATION_COLUMN = "제조경비_기타배부"
# 제조경비_직접의 분자(원천금액) 집계용 부문/배부대상 이름
MANUFACTURING_EXPENSE_DIRECT_TARGET = "직접"


def _manufacturing_expense_target_names():
    return {
        target for _, target, _ in MANUFACTURING_EXPENSE_ALLOCATION_SPECS
    } | {MANUFACTURING_EXPENSE_GIFT_TARGET, MANUFACTURING_EXPENSE_DIRECT_TARGET}


def _aggregate_direct_expense_by_product_id(manufacturing_cost_sheet_dfs):
    """직접경비 시트들에서 (상품ID, 매출구분)별 금액 합계 dict 반환.

    반환: {(상품ID, 매출구분): 금액 합}
    매출구분 컬럼이 없으면 매출구분을 빈 문자열로 처리.
    """
    if not manufacturing_cost_sheet_dfs:
        return {}

    frames = []
    for sheet_name, df in manufacturing_cost_sheet_dfs.items():
        if not str(sheet_name).startswith(DIRECT_EXPENSE_SHEET_PREFIX):
            continue
        if df is None or df.empty or "상품ID" not in df.columns:
            continue

        amount_column = next(
            (c for c in DIRECT_EXPENSE_AMOUNT_COLUMNS if c in df.columns), None
        )
        if amount_column is None:
            continue

        temp = df[["상품ID", amount_column]].copy()
        temp["_매출구분"] = (
            df["매출구분"].astype(str).str.strip()
            if "매출구분" in df.columns
            else ""
        )
        temp = temp.rename(columns={amount_column: "_금액"})
        frames.append(temp)

    if not frames:
        return {}

    combined = pd.concat(frames, ignore_index=True)
    combined["상품ID"] = combined["상품ID"].astype(str).str.strip()
    combined["_매출구분"] = combined["_매출구분"].astype(str).str.strip()
    combined["_금액"] = pd.to_numeric(combined["_금액"], errors="coerce").fillna(0)
    combined = combined[combined["상품ID"].ne("")]
    if combined.empty:
        return {}

    grouped = combined.groupby(["상품ID", "_매출구분"])["_금액"].sum()
    return {(pid, sales_type): amount for (pid, sales_type), amount in grouped.items()}


def _get_manufacturing_expense_totals_from_verification(
    verification_sheets, settlement_year, settlement_month,
):
    """검증시트에서 원가='제조경비'인 행의 부문별/전체 결산월 값을 합산."""
    target_departments = _manufacturing_expense_target_names()
    totals = {target: 0.0 for target in target_departments}
    grand_total = 0.0

    if not verification_sheets:
        return totals, grand_total

    for _, df in verification_sheets.items():
        if df is None or df.empty:
            continue
        if "원가" not in df.columns or "부문" not in df.columns:
            continue

        month_column = _find_settlement_month_column(
            df.columns, settlement_year, settlement_month,
        )
        if month_column is None:
            continue

        temp = df[["원가", "부문", month_column]].copy()
        temp["원가"] = temp["원가"].astype(str).str.strip()
        temp["부문"] = temp["부문"].astype(str).str.strip()
        temp["_금액"] = pd.to_numeric(temp[month_column], errors="coerce").fillna(0)

        manufacturing_rows = temp[temp["원가"].eq("제조경비")]
        if manufacturing_rows.empty:
            continue
        grand_total += float(manufacturing_rows["_금액"].sum())

        matched = manufacturing_rows[manufacturing_rows["부문"].isin(target_departments)]
        if matched.empty:
            continue

        for target, amount in matched.groupby("부문")["_금액"].sum().items():
            totals[target] = totals.get(target, 0.0) + float(amount)

    return totals, grand_total


def _aggregate_department_expense_by_target(manufacturing_cost_sheet_dfs):
    """부문별경비 시트에서 배부대상별/전체 차변 합계 반환."""
    target_names = _manufacturing_expense_target_names()
    totals = {target: 0.0 for target in target_names}
    grand_total = 0.0

    if not manufacturing_cost_sheet_dfs:
        return totals, grand_total

    frames = []
    for sheet_name, df in manufacturing_cost_sheet_dfs.items():
        if not str(sheet_name).startswith(DEPARTMENT_EXPENSE_SHEET_PREFIX):
            continue
        if df is None or df.empty:
            continue
        if "배부대상" not in df.columns or "차변" not in df.columns:
            continue

        temp = df.copy()
        if "원가구분" in temp.columns:
            temp = temp[temp["원가구분"].astype(str).str.strip().eq("제조경비")].copy()
        if temp.empty:
            continue

        frames.append(temp[["배부대상", "차변"]].copy())

    if not frames:
        return totals, grand_total

    combined = pd.concat(frames, ignore_index=True)
    combined["배부대상"] = combined["배부대상"].astype(str).str.strip()
    combined["차변"] = pd.to_numeric(combined["차변"], errors="coerce").fillna(0)
    combined = combined[combined["배부대상"].ne("")]
    if combined.empty:
        return totals, grand_total

    grouped = combined.groupby("배부대상")["차변"].sum()
    grand_total = float(grouped.sum())
    for target, amount in grouped.items():
        if target in target_names:
            totals[target] = float(amount)
    return totals, grand_total


def _allocate_amount_proportional_rounded(
    final_df,
    target_column,
    total_amount,
    weight_series,
    row_mask=None,
    adjust_remainder=False,
):
    """제조경비 전용 비례 배부.

    기본은 행별 반올림만 하고 잔여를 특정 첫 행에 얹지 않는다.
    adjust_remainder=True 인 경우에는 잔여를 소수부 차이가 큰 행부터 1씩 분산해 합계를 맞춘다.
    """
    if total_amount == 0:
        return
    if row_mask is None:
        row_mask = pd.Series(True, index=final_df.index)

    weights = pd.to_numeric(weight_series[row_mask], errors="coerce").fillna(0)
    weights_sum = weights.sum()
    if weights_sum == 0:
        return

    raw_allocations = weights / weights_sum * total_amount
    allocations = raw_allocations.round()

    if adjust_remainder:
        remainder = total_amount - allocations.sum()
        remainder_int = int(round(remainder))
        if remainder_int != 0:
            if remainder_int > 0:
                priority = (raw_allocations - allocations).sort_values(ascending=False)
                step = 1
            else:
                priority = (allocations - raw_allocations).sort_values(ascending=False)
                step = -1

            candidate_indices = [idx for idx in priority.index if weights.loc[idx] != 0]
            if candidate_indices:
                for index in candidate_indices[:abs(remainder_int)]:
                    allocations.loc[index] += step

    # int dtype 컬럼에 float 대입 방지 (pandas 2.x)
    final_df[target_column] = pd.to_numeric(
        final_df[target_column], errors="coerce"
    ).fillna(0).astype(float)
    final_df.loc[row_mask, target_column] = allocations.values


def _round_half_up(value):
    """스칼라 반올림 (0.5 는 올림). 파이썬 기본 round 의 banker's rounding 회피."""
    return int(np.floor(float(value) + 0.5))


def _allocate_amount_by_rounded_unit_rate(
    final_df, target_column, total_amount, weight_series, row_mask=None,
):
    """방식 B: 단가(배부총액 ÷ 가중치합)를 정수로 반올림한 뒤,
    각 행 = 그 행 가중치 × 정수단가 로 배부.

    분모(가중치합)와 개별 가중치는 원래 값(소수 포함) 그대로 사용하고, 단가만 정수 반올림한다.
    합계가 배부총액과 달라질 수 있으며, 그 차액은 호출측에서 기타배부로 처리한다.
    반환: (실제 배부 합계, 정수 단가)
    """
    if row_mask is None:
        row_mask = pd.Series(True, index=final_df.index)

    weights = pd.to_numeric(weight_series[row_mask], errors="coerce").fillna(0)
    weights_sum = weights.sum()
    if weights_sum == 0 or total_amount == 0:
        return 0.0, 0

    unit_rate = _round_half_up(total_amount / weights_sum)
    allocations = weights * unit_rate
    # 대상 컬럼이 int dtype 이면 float 값 대입 시 에러(pandas 2.x) → float 로 캐스팅
    final_df[target_column] = pd.to_numeric(
        final_df[target_column], errors="coerce"
    ).fillna(0).astype(float)
    final_df.loc[row_mask, target_column] = allocations.values

    return float(allocations.sum()), int(unit_rate)


def _append_manufacturing_expense_columns(
    final_df,
    manufacturing_cost_sheet_dfs,
    verification_sheets=None,
    settlement_year=None,
    settlement_month=None,
    diagnostics=None,
):
    """제조경비 컬럼 추가.

    - 제조경비_직접: 직접경비 시트에서 같은 상품ID 의 금액 합계. 검사매출 행은 0
    - 제조경비_임차/전체/RQI/정비/판금/도장:
      (검증시트 원가='제조경비'·부문별 결산월 합 + 부문별경비 배부대상별 차변 합)
      을 sm_일수/rtc_일수/공정별_* 가중치로 비례 배부
    - 제조경비_선물: 같은 방식으로 구한 선물 금액을 분류1='선물' 행의 rtc_일수로 비례 배부
    - 제조경비_기타배부: 원천 총액과 제조경비_직접~제조경비_선물 합계 차이를 rtc_일수로 비례 배부

    diagnostics 가 list 로 주어지면 각 컬럼의 (배부총액=분자 / 가중치합=분모 / 단가) 내역을 append.
    """
    final_df["제조경비_직접"] = 0
    for column_name, _, _ in MANUFACTURING_EXPENSE_ALLOCATION_SPECS:
        final_df[column_name] = 0
    final_df[MANUFACTURING_EXPENSE_GIFT_COLUMN] = 0
    final_df[MANUFACTURING_EXPENSE_EXTRA_ALLOCATION_COLUMN] = 0

    if final_df.empty:
        return final_df

    amount_by_product_id = _aggregate_direct_expense_by_product_id(
        manufacturing_cost_sheet_dfs,
    )
    if amount_by_product_id:
        product_id_series = final_df["상품ID"].astype(str).str.strip()
        sales_type_series = (
            final_df["매출구분"].astype(str).str.strip()
            if "매출구분" in final_df.columns
            else pd.Series([""] * len(final_df), index=final_df.index)
        )
        values = [
            amount_by_product_id.get((pid, sales_type), 0)
            for pid, sales_type in zip(product_id_series, sales_type_series)
        ]
        final_df["제조경비_직접"] = pd.to_numeric(
            pd.Series(values, index=final_df.index), errors="coerce",
        ).fillna(0).astype(int)

        # 검사매출 행은 0
        if "매출구분" in final_df.columns:
            inspection_rows = final_df["매출구분"].astype(str).str.strip().eq("검사매출")
            final_df.loc[inspection_rows, "제조경비_직접"] = 0

    verification_totals, verification_grand_total = _get_manufacturing_expense_totals_from_verification(
        verification_sheets, settlement_year, settlement_month,
    )
    department_expense_totals, department_expense_grand_total = _aggregate_department_expense_by_target(
        manufacturing_cost_sheet_dfs,
    )

    # 제조경비_직접 분자: 검증시트(부문='직접') + 부문별경비(배부대상='직접')
    direct_verification = float(
        verification_totals.get(MANUFACTURING_EXPENSE_DIRECT_TARGET, 0)
    )
    direct_department = float(
        department_expense_totals.get(MANUFACTURING_EXPENSE_DIRECT_TARGET, 0)
    )
    direct_source_total = direct_verification + direct_department
    direct_matched_sum = float(
        pd.to_numeric(final_df["제조경비_직접"], errors="coerce").fillna(0).sum()
    )

    if diagnostics is not None:
        diagnostics.append({
            "컬럼": "제조경비_직접",
            "배부총액(분자)": direct_source_total,
            "실제배부값합": direct_matched_sum,
            "가중치합(분모)": "",
            "단가": "",
            "비고": (
                f"검증시트(부문=직접) {direct_verification:,.0f}"
                f" + 부문별경비(배부대상=직접) {direct_department:,.0f}"
                f" (실제 상품ID+매출구분 매칭 합계 {direct_matched_sum:,.0f})"
            ),
        })

    # 기타배부 계산용 누적: 분자합(각 항목 배부총액)과 실제합(각 항목 실제 배부값)
    # 제조경비_직접도 포함
    numerator_sum = direct_source_total
    actual_sum = direct_matched_sum

    for column_name, target, weight_column in MANUFACTURING_EXPENSE_ALLOCATION_SPECS:
        if weight_column not in final_df.columns:
            continue

        verification_amount = float(verification_totals.get(target, 0))
        department_amount = float(department_expense_totals.get(target, 0))
        allocation_total = verification_amount + department_amount

        # 분모(가중치합): 원래 값 그대로 (실제 계산과 동일)
        weight_sum = float(
            pd.to_numeric(final_df[weight_column], errors="coerce").fillna(0).sum()
        )
        unit_rate = _round_half_up(allocation_total / weight_sum) if weight_sum else 0

        # 기타배부 계산용 누적 (분자)
        numerator_sum += allocation_total

        column_actual = 0.0
        if allocation_total != 0:
            # 방식 B: 정수 단가 × 행 가중치 로 배부
            column_actual, unit_rate = _allocate_amount_by_rounded_unit_rate(
                final_df,
                column_name,
                allocation_total,
                final_df[weight_column],
            )
            actual_sum += column_actual

        if diagnostics is not None:
            diagnostics.append({
                "컬럼": column_name,
                "배부총액(분자)": allocation_total,
                "실제배부값합": column_actual,
                "가중치합(분모)": weight_sum,
                "단가": unit_rate,
                "비고": (
                    f"검증시트 {verification_amount:,.0f} + 부문별경비 {department_amount:,.0f}"
                    f" / 가중치={weight_column} (단가×가중치)"
                ),
            })

    gift_verification = float(verification_totals.get(MANUFACTURING_EXPENSE_GIFT_TARGET, 0))
    gift_department = float(department_expense_totals.get(MANUFACTURING_EXPENSE_GIFT_TARGET, 0))
    gift_total = gift_verification + gift_department
    gift_weight_sum = 0.0
    gift_unit_rate = 0
    gift_actual = 0.0
    if (
        gift_total != 0
        and MANUFACTURING_EXPENSE_GIFT_FILTER_COLUMN in final_df.columns
        and MANUFACTURING_EXPENSE_GIFT_WEIGHT_COLUMN in final_df.columns
    ):
        gift_mask = (
            final_df[MANUFACTURING_EXPENSE_GIFT_FILTER_COLUMN].astype(str).str.strip()
            .eq(MANUFACTURING_EXPENSE_GIFT_FILTER_VALUE)
        )
        if gift_mask.any():
            gift_weight_sum = float(
                pd.to_numeric(
                    final_df.loc[gift_mask, MANUFACTURING_EXPENSE_GIFT_WEIGHT_COLUMN],
                    errors="coerce",
                ).fillna(0).sum()
            )
            # 방식 B: 정수 단가 × 행 가중치
            gift_actual, gift_unit_rate = _allocate_amount_by_rounded_unit_rate(
                final_df,
                MANUFACTURING_EXPENSE_GIFT_COLUMN,
                gift_total,
                final_df[MANUFACTURING_EXPENSE_GIFT_WEIGHT_COLUMN],
                row_mask=gift_mask,
            )

    # 기타배부 계산용 누적 (선물)
    numerator_sum += gift_total
    actual_sum += gift_actual

    if diagnostics is not None:
        diagnostics.append({
            "컬럼": MANUFACTURING_EXPENSE_GIFT_COLUMN,
            "배부총액(분자)": gift_total,
            "실제배부값합": gift_actual,
            "가중치합(분모)": gift_weight_sum,
            "단가": gift_unit_rate,
            "비고": (
                f"검증시트 {gift_verification:,.0f} + 부문별경비 {gift_department:,.0f}"
                f" / 분류1='선물' 행의 {MANUFACTURING_EXPENSE_GIFT_WEIGHT_COLUMN} (단가×가중치)"
            ),
        })

    # 기타배부 분자 = (각 항목 배부총액 분자의 합) − (각 항목 실제 배부값의 합)
    extra_allocation_total = numerator_sum - actual_sum
    extra_weight_sum = 0.0
    if (
        extra_allocation_total != 0
        and "rtc_일수" in final_df.columns
    ):
        extra_weight_sum = float(
            pd.to_numeric(final_df["rtc_일수"], errors="coerce").fillna(0).sum()
        )
        _allocate_amount_proportional_rounded(
            final_df,
            MANUFACTURING_EXPENSE_EXTRA_ALLOCATION_COLUMN,
            extra_allocation_total,
            final_df["rtc_일수"],
            adjust_remainder=True,
        )

    if diagnostics is not None:
        unit_rate = round(extra_allocation_total / extra_weight_sum) if extra_weight_sum else 0
        extra_actual = float(
            pd.to_numeric(
                final_df[MANUFACTURING_EXPENSE_EXTRA_ALLOCATION_COLUMN],
                errors="coerce",
            ).fillna(0).sum()
        )
        diagnostics.append({
            "컬럼": MANUFACTURING_EXPENSE_EXTRA_ALLOCATION_COLUMN,
            "배부총액(분자)": extra_allocation_total,
            "실제배부값합": extra_actual,
            "가중치합(분모)": extra_weight_sum,
            "단가": unit_rate,
            "비고": (
                f"분자합 {numerator_sum:,.0f} - 실제배부합계 {actual_sum:,.0f}"
                f" / 가중치=rtc_일수"
            ),
        })

    return final_df


# ============================================================
# 노무비 컬럼 (노무비_합계 / 전체 / RQI / 정비 / 판금 / 도장 / 선물)
# ============================================================

# 노무비 카테고리: (노무비 컬럼명, 매칭할 배부대상 값, 곱할 공정별 컬럼명)
LABOR_COST_SPECS = [
    ("노무비_전체", "전체", "공정별_전체"),
    ("노무비_RQI",  "RQI",  "공정별_RQI"),
    ("노무비_정비", "정비", "공정별_정비"),
    ("노무비_판금", "판금", "공정별_판금"),
    ("노무비_도장", "도장", "공정별_도장"),
]
LABOR_COST_GIFT_COLUMN = "노무비_선물"
LABOR_COST_GIFT_TARGET = "선물"
LABOR_COST_GIFT_WEIGHT_COLUMN = "rtc_일수"  # 분모 가중치
LABOR_COST_GIFT_FILTER_COLUMN = "분류1"     # 이 컬럼이 '선물' 인 행에만 배부
LABOR_COST_GIFT_FILTER_VALUE = "선물"
LABOR_COST_TOTAL_COLUMN = "노무비_합계"

# 노무비 시트 인식 prefix (제조원가 업로드에서 "노무비" 라벨 사용)
LABOR_COST_SHEET_PREFIX = "노무비"


def _aggregate_labor_cost_by_target(manufacturing_cost_sheet_dfs):
    """노무비 시트들에서 배부대상별 차변 합계 dict 반환.

    반환: {배부대상: 차변 합} (예: {"RQI": 1000, "정비": 2000, "선물": 500, ...})
    """
    if not manufacturing_cost_sheet_dfs:
        return {}

    frames = []
    for sheet_name, df in manufacturing_cost_sheet_dfs.items():
        if not str(sheet_name).startswith(LABOR_COST_SHEET_PREFIX):
            continue
        if df is None or df.empty:
            continue
        if "배부대상" not in df.columns or "차변" not in df.columns:
            continue
        frames.append(df[["배부대상", "차변"]].copy())

    if not frames:
        return {}

    combined = pd.concat(frames, ignore_index=True)
    combined["배부대상"] = combined["배부대상"].astype(str).str.strip()
    combined["차변"] = pd.to_numeric(combined["차변"], errors="coerce").fillna(0)
    combined = combined[combined["배부대상"].ne("")]
    if combined.empty:
        return {}

    return dict(combined.groupby("배부대상")["차변"].sum())


def _append_labor_cost_columns(final_df, manufacturing_cost_sheet_dfs):
    """노무비_합계 / 전체 / RQI / 정비 / 판금 / 도장 / 선물 컬럼 추가.

    공통 (전체/RQI/정비/판금/도장):
        - 노무비 시트의 배부대상별 차변 합산 = 배부할 총액
        - 그 총액을 공정별_{카테고리} 값에 비례하여 행별로 배부 (정수, 잔여는 첫 행)

    노무비_선물 (다른 규칙):
        - 분자: 노무비 시트 배부대상=='선물' 차변 합
        - 분모/가중치: 분류1=='선물' 인 행의 rtc_일수
        - 분류1=='선물' 이 아닌 행은 0

    노무비_합계 = 위 6개 행별 합
    """
    # 0 으로 초기화 (정수 dtype)
    for column_name, _, _ in LABOR_COST_SPECS:
        final_df[column_name] = 0
    final_df[LABOR_COST_GIFT_COLUMN] = 0
    final_df[LABOR_COST_TOTAL_COLUMN] = 0

    if final_df.empty:
        return final_df

    labor_total_by_target = _aggregate_labor_cost_by_target(manufacturing_cost_sheet_dfs)

    # 1) 공정별 카테고리 (전체/RQI/정비/판금/도장)
    for column_name, allocation_target, process_column in LABOR_COST_SPECS:
        if process_column not in final_df.columns:
            continue

        labor_total = float(labor_total_by_target.get(allocation_target, 0))
        if labor_total == 0:
            continue

        _allocate_amount_proportional(
            final_df,
            column_name,
            labor_total,
            final_df[process_column],
        )

    # 2) 노무비_선물 (분류1=='선물' 행의 rtc_일수 비율로 배부)
    gift_labor_total = float(labor_total_by_target.get(LABOR_COST_GIFT_TARGET, 0))
    if (
        gift_labor_total != 0
        and LABOR_COST_GIFT_FILTER_COLUMN in final_df.columns
        and LABOR_COST_GIFT_WEIGHT_COLUMN in final_df.columns
    ):
        gift_mask = (
            final_df[LABOR_COST_GIFT_FILTER_COLUMN].astype(str).str.strip()
            .eq(LABOR_COST_GIFT_FILTER_VALUE)
        )
        if gift_mask.any():
            _allocate_amount_proportional(
                final_df,
                LABOR_COST_GIFT_COLUMN,
                gift_labor_total,
                final_df[LABOR_COST_GIFT_WEIGHT_COLUMN],
                row_mask=gift_mask,
            )

    # 3) 노무비_합계 = 6개 합
    component_columns = [c for c, _, _ in LABOR_COST_SPECS] + [LABOR_COST_GIFT_COLUMN]
    final_df[LABOR_COST_TOTAL_COLUMN] = sum(
        pd.to_numeric(final_df[c], errors="coerce").fillna(0)
        for c in component_columns
    )

    return final_df


# ============================================================
# 공정별 측정시간 컬럼 (공정별_전체 / RQI / 정비 / 판금 / 도장)
# ============================================================

# 공정별 컬럼명 → 매칭할 '구분' 값
PROCESS_CATEGORY_COLUMNS = {
    "공정별_RQI": "RQI",
    "공정별_정비": "정비",
    "공정별_판금": "판금",
    "공정별_도장": "도장",
}
PROCESS_CATEGORY_TOTAL_COLUMN = "공정별_전체"


def _aggregate_process_hours(combined_cost_driver_df):
    """원가동인 통합 DataFrame 을 (상품ID, 매출구분, 구분) 별 측정시간(H) 합계로 집계.

    반환: {(상품ID, 매출구분, 구분): 측정시간 합} dict
    """
    if combined_cost_driver_df is None or combined_cost_driver_df.empty:
        return {}

    required = {"상품ID", "매출구분", "구분", "측정시간(H)"}
    if not required.issubset(combined_cost_driver_df.columns):
        return {}

    df = combined_cost_driver_df[["상품ID", "매출구분", "구분", "측정시간(H)"]].copy()
    df["상품ID"] = df["상품ID"].astype(str).str.strip()
    df["매출구분"] = df["매출구분"].astype(str).str.strip()
    df["구분"] = df["구분"].astype(str).str.strip()
    df["측정시간(H)"] = pd.to_numeric(df["측정시간(H)"], errors="coerce").fillna(0)

    df = df[df["상품ID"].ne("") & df["매출구분"].ne("") & df["구분"].ne("")]
    if df.empty:
        return {}

    grouped = df.groupby(["상품ID", "매출구분", "구분"])["측정시간(H)"].sum()
    return grouped.to_dict()


def _append_process_category_columns(final_df, combined_cost_driver_df):
    """공정별_전체 / RQI / 정비 / 판금 / 도장 컬럼 추가.

    매칭: (상품ID, 매출구분, 구분) 일치하는 측정시간(H) 합산 (SUMIF 와 동일)
    예외:
        - 매출구분 == '검사매출' 행은 공정별_정비/판금/도장 = 0
        - 공정별_RQI 는 검사매출이면 0.5 고정
    공정별_전체 = 공정별_RQI + 공정별_정비 + 공정별_판금 + 공정별_도장 (항상 단순 합)
    """
    # 0.0 으로 초기화 (float dtype 으로 — 검사매출 행에 0.5 가 들어갈 수 있으므로)
    for column in PROCESS_CATEGORY_COLUMNS.keys():
        final_df[column] = 0.0
    final_df[PROCESS_CATEGORY_TOTAL_COLUMN] = 0.0

    if final_df.empty or "매출구분" not in final_df.columns:
        return final_df

    hours_map = _aggregate_process_hours(combined_cost_driver_df)

    product_id_series = final_df["상품ID"].astype(str).str.strip()
    sales_type_series = final_df["매출구분"].astype(str).str.strip()
    inspection_mask = sales_type_series.eq("검사매출")

    # 각 공정별 컬럼 채우기
    for column_name, category in PROCESS_CATEGORY_COLUMNS.items():
        values = [
            float(hours_map.get((pid, st_type, category), 0))
            for pid, st_type in zip(product_id_series, sales_type_series)
        ]
        final_df[column_name] = pd.Series(values, index=final_df.index, dtype=float)

    # 검사매출 행 처리: 모든 공정별 컬럼 0
    for column in PROCESS_CATEGORY_COLUMNS.keys():
        final_df.loc[inspection_mask, column] = 0
    # 단, 공정별_RQI 는 검사매출일 때 0.5 고정
    final_df.loc[inspection_mask, "공정별_RQI"] = 0.5

    # 공정별_전체 = 4개 합
    final_df[PROCESS_CATEGORY_TOTAL_COLUMN] = sum(
        pd.to_numeric(final_df[c], errors="coerce").fillna(0)
        for c in PROCESS_CATEGORY_COLUMNS.keys()
    )

    return final_df


# ============================================================
# 원가동인 컬럼 (rtc_일수 / sm_일수)
# ============================================================

def _pick_sheet_for_settlement_month(sheet_map, settlement_year, settlement_month):
    """시트 dict 에서 결산연도-월에 해당하는 시트 선택.

    1) 정규화된 시트명이 후보 집합과 일치하면 선택
       후보: '2026-01', '2026-1', '2026/01', '2026.01', '202601', '2026년 1월', '1월', '01' 등
    2) 시트명이 'YYYY-MM' 또는 'YYYY-M' 으로 시작하면 선택 (예: '2026-01_상세')
    """
    if not sheet_map or settlement_year is None or settlement_month is None:
        return None

    year = int(settlement_year)
    month = int(settlement_month)
    candidates_raw = {
        f"{year}-{month:02d}", f"{year}-{month}",
        f"{year}/{month:02d}", f"{year}/{month}",
        f"{year}.{month:02d}", f"{year}.{month}",
        f"{year}{month:02d}",
        f"{year}년 {month}월", f"{year}년{month}월",
        f"{year}년 {month:02d}월",
        f"{month}월", f"{month:02d}월",
        str(month), f"{month:02d}",
    }
    candidates = {re.sub(r"\s+", "", c) for c in candidates_raw}
    prefix_short = f"{year}-{month}"
    prefix_zero = f"{year}-{month:02d}"

    for sheet_name, df in sheet_map.items():
        normalized = re.sub(r"\s+", "", str(sheet_name).strip())
        if normalized in candidates:
            return df
        if normalized.startswith(prefix_zero) or normalized.startswith(prefix_short):
            return df

    return None


def _aggregate_cost_driver_by_product_id(
    cost_driver_dfs, keyword, day_column, settlement_year, settlement_month,
):
    """cost_driver_dfs[keyword] 의 결산월 시트에서 상품ID별 day_column 값 합산."""
    if not cost_driver_dfs:
        return None
    sheet_map = cost_driver_dfs.get(keyword)
    if sheet_map is None:
        return None

    # 하위호환: 값이 DataFrame 으로 들어온 경우 단일 시트 dict 로 변환
    if isinstance(sheet_map, pd.DataFrame):
        sheet_map = {keyword: sheet_map}
    if not isinstance(sheet_map, dict) or len(sheet_map) == 0:
        return None

    df = _pick_sheet_for_settlement_month(sheet_map, settlement_year, settlement_month)
    if df is None or df.empty:
        return None
    if "상품ID" not in df.columns or day_column not in df.columns:
        return None

    temp = df[["상품ID", day_column]].copy()
    temp["상품ID"] = temp["상품ID"].astype(str).str.strip()
    temp[day_column] = pd.to_numeric(temp[day_column], errors="coerce").fillna(0)
    temp = temp[temp["상품ID"].ne("")].copy()
    if temp.empty:
        return None

    return temp.groupby("상품ID", as_index=False)[day_column].sum()


def _append_cost_driver_columns(
    final_df, cost_driver_dfs, settlement_year, settlement_month,
):
    """rtc_일수 / sm_일수 컬럼 부여.

    - cost_driver_dfs 구조: {keyword: {sheet_name: df}} — 결산연도-월 시트만 사용
    - 컬럼명 형식: '{year}-{month:02d}_일수' (예: '2026-01_일수')
    - rtc_일수: 매출구분이 '검사매출' 이면 0, 그 외 행만 매칭
    - sm_일수: 매출구분이 '검사매출' 또는 '정비매출' 이면 0, 그 외 행만 매칭
    """
    final_df["rtc_일수"] = 0
    final_df["sm_일수"] = 0

    if settlement_year is None or settlement_month is None:
        return final_df

    day_column = f"{int(settlement_year)}-{int(settlement_month):02d}_일수"

    # excluded_sales_types: 해당 매출구분이면 값을 0 으로 (즉, 가져오지 않음)
    cost_driver_specs = [
        ("rtc", "rtc_일수", {"검사매출"}),
        ("sm", "sm_일수", {"검사매출", "정비매출"}),
    ]

    for keyword, target_column, excluded_sales_types in cost_driver_specs:
        aggregated_df = _aggregate_cost_driver_by_product_id(
            cost_driver_dfs, keyword, day_column,
            settlement_year, settlement_month,
        )
        if aggregated_df is None or aggregated_df.empty:
            continue

        aggregated_df = aggregated_df.rename(columns={day_column: target_column})
        final_df = final_df.drop(columns=[target_column])
        final_df = _merge_by_product_id(final_df, aggregated_df, [target_column])
        final_df[target_column] = pd.to_numeric(
            final_df[target_column], errors="coerce"
        ).fillna(0)

        if "매출구분" in final_df.columns:
            excluded = (
                final_df["매출구분"].astype(str).str.strip().isin(excluded_sales_types)
            )
            final_df.loc[excluded, target_column] = 0

    return final_df


# ============================================================
# 컬럼 순서 정렬
# ============================================================

def _append_total_purchase_cost_columns(final_df):
    """FINAL_COST_MONTHLY_COLUMNS 8개를 합산해 매입원가/매입원가_전월/매입원가_합계 컬럼 추가."""
    for suffix in ("", "_전월", "_합계"):
        component_columns = [
            f"{column}{suffix}" for column in FINAL_COST_MONTHLY_COLUMNS
        ]
        existing = [c for c in component_columns if c in final_df.columns]
        target_column = f"{TOTAL_PURCHASE_COST_COLUMN}{suffix}"

        if existing:
            final_df[target_column] = sum(
                pd.to_numeric(final_df[c], errors="coerce").fillna(0) for c in existing
            )
        else:
            final_df[target_column] = 0
    return final_df


def _append_cost_group_cumulative_columns(
    final_df, inventory_df, settlement_month,
):
    """재료비 / 노무비 / 제조경비 의 누적합계·전월누적·당월 컬럼 부여.

    당월값:
        재료비_당월 = 재료비_합, 노무비_당월 = 노무비_합계, 제조경비_당월 = 제조경비_합계
    전월누적:
        기초재고에서 전월 기말 행의 '{그룹}_전월' 컬럼값 (초과운행_전월 등과 동일 방식)
    누적합계:
        전월누적 + 당월
    """
    group_specs = [
        ("재료비", "재료비_합"),
        ("노무비", LABOR_COST_TOTAL_COLUMN),       # 노무비_합계
        ("제조경비", "제조경비_합계"),
    ]

    # 1) 당월값 + 전월누적 0 초기화
    for group_name, source_column in group_specs:
        current_column = f"{group_name}_당월"
        previous_column = f"{group_name}_전월누적"
        if source_column in final_df.columns:
            final_df[current_column] = pd.to_numeric(
                final_df[source_column], errors="coerce"
            ).fillna(0)
        else:
            final_df[current_column] = 0
        final_df[previous_column] = 0

    # 2) 전월누적: 기초재고에서 전월 기말 행의 '{그룹}_전월' 값 가져오기
    if (
        inventory_df is not None
        and not inventory_df.empty
        and "상품ID" in inventory_df.columns
    ):
        inventory = _strip_columns(inventory_df).copy()

        if settlement_month is not None:
            previous_month = 12 if int(settlement_month) == 1 else int(settlement_month) - 1
            previous_flag_column = f"{previous_month}월 기말여부"
            if previous_flag_column in inventory.columns:
                inventory = inventory[_is_flag_one(inventory[previous_flag_column])].copy()
            else:
                inventory = inventory.iloc[0:0].copy()

        inventory["상품ID"] = inventory["상품ID"].astype(str).str.strip()
        inventory = inventory[inventory["상품ID"].ne("")].copy()

        # 기초재고 시트의 전월 컬럼명: '{그룹}_전월'
        source_to_target = {
            f"{group_name}_전월": f"{group_name}_전월누적"
            for group_name, _ in group_specs
        }
        available_sources = [
            src for src in source_to_target if src in inventory.columns
        ]

        if available_sources and not inventory.empty:
            for src in available_sources:
                inventory[src] = pd.to_numeric(
                    inventory[src], errors="coerce"
                ).fillna(0)
            previous_df = (
                inventory.groupby("상품ID", as_index=False)[available_sources].sum()
            )
            previous_df = previous_df.rename(columns=source_to_target)
            target_columns = [source_to_target[src] for src in available_sources]

            final_df = final_df.drop(columns=target_columns)
            final_df = _merge_by_product_id(final_df, previous_df, target_columns)
            for col in target_columns:
                final_df[col] = pd.to_numeric(
                    final_df[col], errors="coerce"
                ).fillna(0)

    # 3) 누적합계 = 전월누적 + 당월
    for group_name, _ in group_specs:
        final_df[f"{group_name}_누적합계"] = (
            pd.to_numeric(final_df[f"{group_name}_전월누적"], errors="coerce").fillna(0)
            + pd.to_numeric(final_df[f"{group_name}_당월"], errors="coerce").fillna(0)
        )

    # 4) 제조원가 = 재료비 + 노무비 + 제조경비 (각 누적합계/전월누적/당월 합산)
    for suffix in ("_누적합계", "_전월누적", "_당월"):
        final_df[f"제조원가{suffix}"] = sum(
            pd.to_numeric(final_df[f"{group_name}{suffix}"], errors="coerce").fillna(0)
            for group_name, _ in group_specs
        )

    return final_df


def _reorder_final_columns(
    final_df,
    manufacturing_cost_sheet_dfs=None,
    verification_sheets=None,
    settlement_year=None,
    settlement_month=None,
    cost_driver_dfs=None,
    combined_cost_driver_df=None,
    inventory_df=None,
):
    """매입원가 + 항목별 [합계, 전월, 당월] + 재료비 + 원가동인 + 공정별 컬럼 정렬.

    배치 순서:
        ... 일반 컬럼 ...
        매입원가_합계, 매입원가_전월, 매입원가,
        상품매입액_합계, 상품매입액_전월, 상품매입액,
        ... (FINAL_COST_MONTHLY_COLUMNS 순서대로)
        재료비_합, 재료비_직접, 재료비_배부,
        rtc_일수, sm_일수,
        공정별_전체, 공정별_RQI, 공정별_정비, 공정별_판금, 공정별_도장
    """
    final_df = _append_total_purchase_cost_columns(final_df)
    final_df = _append_material_cost_columns(
        final_df, manufacturing_cost_sheet_dfs,
        verification_sheets=verification_sheets,
        settlement_year=settlement_year,
        settlement_month=settlement_month,
    )
    final_df = _append_cost_driver_columns(
        final_df, cost_driver_dfs, settlement_year, settlement_month,
    )
    final_df = _append_process_category_columns(final_df, combined_cost_driver_df)
    # 노무비는 공정별 컬럼이 채워진 후에 계산해야 함 (분모로 공정별_*_합 사용)
    final_df = _append_labor_cost_columns(final_df, manufacturing_cost_sheet_dfs)
    manufacturing_expense_diagnostics = []
    final_df = _append_manufacturing_expense_columns(
        final_df,
        manufacturing_cost_sheet_dfs,
        verification_sheets=verification_sheets,
        settlement_year=settlement_year,
        settlement_month=settlement_month,
        diagnostics=manufacturing_expense_diagnostics,
    )
    # 진단 내역을 결과 DataFrame 의 attrs 에 저장 (UI 에서 표시용)
    final_df.attrs["제조경비_배부내역"] = manufacturing_expense_diagnostics

    # 제조경비_합계 = 직접 + 임차 + 전체 + RQI + 정비 + 판금 + 도장 + 선물 + 기타배부
    manufacturing_expense_all_columns = (
        ["제조경비_직접"]
        + [name for name, _, _ in MANUFACTURING_EXPENSE_ALLOCATION_SPECS]
        + [MANUFACTURING_EXPENSE_GIFT_COLUMN]
        + [MANUFACTURING_EXPENSE_EXTRA_ALLOCATION_COLUMN]
    )
    final_df["제조경비_합계"] = sum(
        pd.to_numeric(final_df[c], errors="coerce").fillna(0)
        for c in manufacturing_expense_all_columns
        if c in final_df.columns
    )

    # 재료비/노무비/제조경비 누적합계·전월누적·당월 컬럼 (제조경비_합계 계산 후)
    final_df = _append_cost_group_cumulative_columns(
        final_df, inventory_df, settlement_month,
    )

    tail_columns = [
        f"{TOTAL_PURCHASE_COST_COLUMN}_합계",
        f"{TOTAL_PURCHASE_COST_COLUMN}_전월",
        TOTAL_PURCHASE_COST_COLUMN,
        # 매입원가_당월 뒤: 제조원가 누적합계/전월누적/당월
        "제조원가_누적합계",
        "제조원가_전월누적",
        "제조원가_당월",
    ]
    for column in FINAL_COST_MONTHLY_COLUMNS:
        tail_columns.extend([f"{column}_합계", f"{column}_전월", column])
    # 초과운행_당월(EXCESS_DRIVING_COLUMN) 뒤: 재료비/노무비/제조경비 누적 묶음
    for group_name in ("재료비", "노무비", "제조경비"):
        tail_columns.extend([
            f"{group_name}_누적합계",
            f"{group_name}_전월누적",
            f"{group_name}_당월",
        ])
    tail_columns.extend(["재료비_합", "재료비_직접", "재료비_배부"])
    tail_columns.extend(["rtc_일수", "sm_일수"])
    tail_columns.extend([PROCESS_CATEGORY_TOTAL_COLUMN])
    tail_columns.extend(PROCESS_CATEGORY_COLUMNS.keys())
    tail_columns.extend([LABOR_COST_TOTAL_COLUMN])
    tail_columns.extend([name for name, _, _ in LABOR_COST_SPECS])
    tail_columns.extend([LABOR_COST_GIFT_COLUMN])
    tail_columns.extend(["제조경비_합계"])
    tail_columns.extend(["제조경비_직접"])
    tail_columns.extend([name for name, _, _ in MANUFACTURING_EXPENSE_ALLOCATION_SPECS])
    tail_columns.extend([MANUFACTURING_EXPENSE_GIFT_COLUMN])
    tail_columns.extend([MANUFACTURING_EXPENSE_EXTRA_ALLOCATION_COLUMN])

    tail_columns = [c for c in tail_columns if c in final_df.columns]
    base_columns = [c for c in final_df.columns if c not in tail_columns]
    final_df = final_df[base_columns + tail_columns]

    # 최종 출력 컬럼명으로 변경 (계산은 내부 이름으로 끝났고 표시용만 rename)
    final_df = final_df.rename(columns=_build_output_column_rename_map())
    return final_df


def _build_output_column_rename_map():
    """내부 계산용 컬럼명 → 최종 출력 컬럼명 매핑.

    계산 로직에서 쓰는 컬럼명은 그대로 두고, 최종 결과 표시에만 사용한다.
    """
    rename_map = {}

    # 매입원가: 누적합계 / 전월누적 / 당월
    rename_map[f"{TOTAL_PURCHASE_COST_COLUMN}_합계"] = f"{TOTAL_PURCHASE_COST_COLUMN}_누적합계"
    rename_map[f"{TOTAL_PURCHASE_COST_COLUMN}_전월"] = f"{TOTAL_PURCHASE_COST_COLUMN}_전월누적"
    rename_map[TOTAL_PURCHASE_COST_COLUMN] = f"{TOTAL_PURCHASE_COST_COLUMN}_당월"

    # 항목별 (상품매입액 ~ 초과운행): 당월값에 _당월 접미사 (합계/전월은 유지)
    for column in FINAL_COST_MONTHLY_COLUMNS:
        rename_map[column] = f"{column}_당월"

    # 재료비
    rename_map["재료비_합"] = "당월_재료비_합계"
    rename_map["재료비_직접"] = "당월_재료비_직접"
    rename_map["재료비_배부"] = "당월_재료비_배부"

    # 원가동인
    rename_map["rtc_일수"] = "RTC_일수"
    rename_map["sm_일수"] = "SM_일수"

    # 공정별 → 유효실측시간
    rename_map[PROCESS_CATEGORY_TOTAL_COLUMN] = "유효실측시간_전체"
    rename_map["공정별_RQI"] = "유효실측시간_RQI"
    rename_map["공정별_정비"] = "유효실측시간_정비"
    rename_map["공정별_판금"] = "유효실측시간_판금"
    rename_map["공정별_도장"] = "유효실측시간_도장"

    # 노무비 → 당월_노무비_*
    rename_map[LABOR_COST_TOTAL_COLUMN] = "당월_노무비_합계"
    rename_map["노무비_전체"] = "당월_노무비_전체"
    rename_map["노무비_RQI"] = "당월_노무비_RQI"
    rename_map["노무비_정비"] = "당월_노무비_정비"
    rename_map["노무비_판금"] = "당월_노무비_판금"
    rename_map["노무비_도장"] = "당월_노무비_도장"
    rename_map[LABOR_COST_GIFT_COLUMN] = "당월_노무비_선물"

    # 제조경비 → 당월_제조경비_*
    rename_map["제조경비_합계"] = "당월_제조경비_합계"
    rename_map["제조경비_직접"] = "당월_제조경비_직접"
    rename_map["제조경비_임차"] = "당월_제조경비_임차료"
    rename_map["제조경비_전체"] = "당월_제조경비_전체"
    rename_map["제조경비_RQI"] = "당월_제조경비_RQI"
    rename_map["제조경비_정비"] = "당월_제조경비_정비"
    rename_map["제조경비_판금"] = "당월_제조경비_판금"
    rename_map["제조경비_도장"] = "당월_제조경비_도장"
    rename_map[MANUFACTURING_EXPENSE_GIFT_COLUMN] = "당월_제조경비_선물"
    rename_map[MANUFACTURING_EXPENSE_EXTRA_ALLOCATION_COLUMN] = "당월_제조경비_기타배부"

    return rename_map


# ============================================================
# 메인 빌더
# ============================================================

def build_final_cost_df(
    product_id_df,
    purchase_cost_sheet_dfs,
    inventory_df=None,
    settlement_month=None,
    manufacturing_cost_sheet_dfs=None,
    verification_sheets=None,
    settlement_year=None,
    cost_driver_dfs=None,
    combined_cost_driver_df=None,
):
    final_df = product_id_df.copy()

    for column in FINAL_COST_AMOUNT_COLUMNS:
        final_df[column] = 0
    final_df[DIFFERENCE_ALLOCATION_COLUMN] = 0

    if final_df.empty:
        return final_df

    reorder_kwargs = dict(
        manufacturing_cost_sheet_dfs=manufacturing_cost_sheet_dfs,
        verification_sheets=verification_sheets,
        settlement_year=settlement_year,
        settlement_month=settlement_month,
        cost_driver_dfs=cost_driver_dfs,
        combined_cost_driver_df=combined_cost_driver_df,
        inventory_df=inventory_df,
    )

    # 매입원가 시트가 없으면 전월 컬럼만 채우고 종료
    if not purchase_cost_sheet_dfs:
        final_df = _append_previous_month_cost_columns(final_df, inventory_df, settlement_month)
        return _reorder_final_columns(final_df, **reorder_kwargs)

    # 1) 상품원장에서 집계
    cost_totals, transfer_in_map, purchase_amount_df = (
        _extract_product_ledger_aggregates(purchase_cost_sheet_dfs)
    )

    if purchase_amount_df is not None:
        final_df = final_df.drop(columns=PURCHASE_AMOUNT_COLUMNS)
        final_df = _merge_by_product_id(final_df, purchase_amount_df, PURCHASE_AMOUNT_COLUMNS)
        for column in PURCHASE_AMOUNT_COLUMNS:
            final_df[column] = pd.to_numeric(final_df[column], errors="coerce").fillna(0)

    # 2) 폐자원공제 시트
    waste_df = _aggregate_sheet_amounts_by_product_id(
        purchase_cost_sheet_dfs,
        sheet_prefix=WASTE_RESOURCE_COLUMN,
        amount_column_candidates=WASTE_RESOURCE_AMOUNT_COLUMNS,
        output_column=WASTE_RESOURCE_COLUMN,
        apply_exclusion=True,
    )
    final_df = _apply_aggregated_amount(final_df, waste_df, WASTE_RESOURCE_COLUMN)

    # 3) 페이백(반납) 시트
    payback_df = _aggregate_sheet_amounts_by_product_id(
        purchase_cost_sheet_dfs,
        sheet_prefix="페이백",
        amount_column_candidates=PAYBACK_RETURN_AMOUNT_COLUMNS,
        output_column=PAYBACK_RETURN_COLUMN,
        apply_exclusion=False,
    )
    final_df = _apply_aggregated_amount(final_df, payback_df, PAYBACK_RETURN_COLUMN)

    # 숫자화
    for column in FINAL_COST_AMOUNT_COLUMNS:
        final_df[column] = pd.to_numeric(final_df[column], errors="coerce").fillna(0)

    # 4) 타사차량 행은 모든 금액 0
    own_vehicle_rows = None
    if "당사/타사" in final_df.columns:
        own_vehicle_rows = final_df["당사/타사"].astype(str).str.strip().eq("당사차량")
        final_df.loc[~own_vehicle_rows, FINAL_COST_AMOUNT_COLUMNS] = 0

    # 5) 차액배부: 당사 정상입고 행에 배부
    if own_vehicle_rows is not None and "정상입고" in final_df.columns:
        difference_total = sum(
            cost_totals.get(column, 0)
            - (pd.to_numeric(final_df[column], errors="coerce").fillna(0).sum()
               if column in final_df.columns else 0)
            for column in DIFFERENCE_SOURCE_COST_COLUMNS
        )
        _allocate_amount(
            final_df,
            DIFFERENCE_ALLOCATION_COLUMN,
            difference_total,
            own_vehicle_rows & _normal_inbound_mask(final_df),
        )

    # 6) 페이백(미반납) / 초과운행: 선물 정상입고 행에 배부
    if "분류1" in final_df.columns and "정상입고" in final_df.columns:
        gift_inbound_mask = (
            final_df["분류1"].astype(str).str.strip().eq("선물")
            & _normal_inbound_mask(final_df)
        )

        payback_unreturned_total = (
            cost_totals.get(PAYBACK_RETURN_COLUMN, 0)
            - pd.to_numeric(final_df[PAYBACK_RETURN_COLUMN], errors="coerce").fillna(0).sum()
            + cost_totals.get(PAYBACK_UNRETURNED_COLUMN, 0)
        )
        _allocate_amount(
            final_df, PAYBACK_UNRETURNED_COLUMN, payback_unreturned_total, gift_inbound_mask
        )

        excess_driving_total = cost_totals.get(EXCESS_DRIVING_COLUMN, 0)
        _allocate_amount(
            final_df, EXCESS_DRIVING_COLUMN, excess_driving_total, gift_inbound_mask
        )

    # 7) 전월/합계 컬럼
    final_df = _append_previous_month_cost_columns(
        final_df, inventory_df, settlement_month,
        transfer_in_amount_map=transfer_in_map,
    )

    return _reorder_final_columns(final_df, **reorder_kwargs)