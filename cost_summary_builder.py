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

# 제조경비 배부 내역을 모듈 레벨에도 저장 (df.attrs 가 pandas 연산/캐시로 사라질 때 대비)
_LAST_MANUFACTURING_EXPENSE_DIAGNOSTICS = []
_LAST_MATERIAL_ALLOCATION_DIAGNOSTICS = []


def get_last_manufacturing_expense_diagnostics():
    """가장 최근 build 의 제조경비 배부 내역 반환 (df.attrs 백업용)."""
    return _LAST_MANUFACTURING_EXPENSE_DIAGNOSTICS


def get_last_material_allocation_diagnostics():
    """가장 최근 build 의 재료비 배부 내역 반환 (df.attrs 백업용)."""
    return _LAST_MATERIAL_ALLOCATION_DIAGNOSTICS

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
    _excel_round,
    _excel_round_series,
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
    settlement_year=None,
    settlement_month=None,
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

        # 회계연도/회계월 필터 (settlement_year/month 참촘서)
        temp = df.copy()
        if settlement_year is not None and "회계연도" in temp.columns:
            temp = temp[pd.to_numeric(temp["회계연도"], errors="coerce").eq(int(settlement_year))]
        if settlement_month is not None and "회계월" in temp.columns:
            temp = temp[pd.to_numeric(temp["회계월"], errors="coerce").eq(int(settlement_month))]
        if temp.empty:
            continue

        selected_columns = ["상품ID", amount_column]
        if apply_exclusion and "제외대상" in temp.columns:
            selected_columns.append("제외대상")

        temp = temp[selected_columns].copy()
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

def _aggregate_ledger_by_cost_type(purchase_cost_sheet_dfs, cost_type):
    """상품원장에서 원가구분 == cost_type 인 행의 상품ID별 (개수, 금액합) 반환.

    반환: {상품ID: {"count": 개수, "amount": 금액합}}
    """
    result = {}
    if not purchase_cost_sheet_dfs:
        return result

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

    if not frames:
        return result

    ledger = pd.concat(frames, ignore_index=True)
    ledger["상품ID"] = ledger["상품ID"].astype(str).str.strip()
    ledger["원가구분"] = ledger["원가구분"].astype(str).str.strip()
    ledger["금액"] = pd.to_numeric(ledger["금액"], errors="coerce").fillna(0)

    matched = ledger[ledger["원가구분"].eq(cost_type) & ledger["상품ID"].ne("")]
    if matched.empty:
        return result

    grouped = matched.groupby("상품ID")["금액"].agg(["count", "sum"])
    for product_id, row in grouped.iterrows():
        result[product_id] = {"count": int(row["count"]), "amount": float(row["sum"])}
    return result


def _extract_product_ledger_aggregates(purchase_cost_sheet_dfs, settlement_year=None, settlement_month=None):
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
        _df = df.copy()
        if settlement_year is not None and "회계연도" in _df.columns:
            _df = _df[pd.to_numeric(_df["회계연도"], errors="coerce").eq(int(settlement_year))]
        if settlement_month is not None and "회계월" in _df.columns:
            _df = _df[pd.to_numeric(_df["회계월"], errors="coerce").eq(int(settlement_month))]
        if _df.empty:
            continue
        frames.append(_df[required_columns].copy())

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
        return 0.0, 0.0
    if row_mask is None:
        row_mask = pd.Series(True, index=final_df.index)

    weights = pd.to_numeric(weight_series[row_mask], errors="coerce").fillna(0)
    weights_sum = weights.sum()
    if weights_sum == 0:
        return 0.0, float(weights_sum)

    # float64 나눗셈/곱셈 오차 방지: Decimal로 각 행 계산
    from decimal import Decimal, ROUND_HALF_UP
    _total = Decimal(str(total_amount))
    _wsum = Decimal(str(weights_sum))
    def _dec_alloc(w):
        if w != w or w == 0:
            return 0
        result = Decimal(str(w)) / _wsum * _total
        return int(result.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    allocations = weights.apply(_dec_alloc)
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

    actual_sum = pd.to_numeric(
        final_df.loc[row_mask, target_column], errors="coerce"
    ).fillna(0).sum()
    return float(actual_sum), float(weights_sum)


def _adjust_material_allocation_to_target(final_df, target_total, row_mask, weight_series):
    """재료비_배부 합계가 목표 금액과 다르면 첫 배부 가능 행에 차이를 보정."""
    if row_mask is None:
        row_mask = pd.Series(True, index=final_df.index)

    final_df["재료비_배부"] = pd.to_numeric(
        final_df["재료비_배부"], errors="coerce"
    ).fillna(0).astype(float)

    values = pd.to_numeric(final_df.loc[row_mask, "재료비_배부"], errors="coerce").fillna(0)
    current_total = float(values.sum())
    remainder = float(target_total) - current_total
    if abs(remainder) < 1e-9:
        return current_total, 0.0

    candidate_indices = list(values[values.ne(0)].index)
    if not candidate_indices:
        weights = pd.to_numeric(weight_series[row_mask], errors="coerce").fillna(0)
        candidate_indices = list(weights[weights.ne(0)].index)
    if not candidate_indices:
        candidate_indices = list(final_df.index[row_mask])
    if not candidate_indices:
        return current_total, 0.0

    final_df.loc[candidate_indices[0], "재료비_배부"] += remainder
    adjusted_total = float(
        pd.to_numeric(final_df.loc[row_mask, "재료비_배부"], errors="coerce").fillna(0).sum()
    )
    return adjusted_total, remainder


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


def _aggregate_material_cost_by_product_id(manufacturing_cost_sheet_dfs, settlement_year=None, settlement_month=None):
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

        # 회계연도/회계월 필터
        _df = df.copy()
        if settlement_year is not None and "회계연도" in _df.columns:
            _df = _df[pd.to_numeric(_df["회계연도"], errors="coerce").eq(int(settlement_year))]
        if settlement_month is not None and "회계월" in _df.columns:
            _df = _df[pd.to_numeric(_df["회계월"], errors="coerce").eq(int(settlement_month))]
        if _df.empty:
            continue

        temp = _df[["상품ID", "매출구분", amount_column]].copy()
        temp["매출구분"] = temp["매출구분"].astype(str).str.strip()
        temp = temp[~temp["매출구분"].eq("검사매출")]
        # 원가구분 == '재료비' 만 직접에 포함 (페인트는 별도 컬럼으로 분리)
        if "원가구분" in df.columns:
            cost_type = df["원가구분"].astype(str).str.strip()
            temp = temp[cost_type.loc[temp.index].eq("재료비")]
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


def _aggregate_paint_amount(manufacturing_cost_sheet_dfs, settlement_year=None, settlement_month=None):
    """재료비 시트에서 원가구분 == '페인트' 인 행의 금액 합 반환."""
    if not manufacturing_cost_sheet_dfs:
        return 0.0

    total = 0.0
    for sheet_name, df in manufacturing_cost_sheet_dfs.items():
        if not str(sheet_name).startswith(MATERIAL_COST_SHEET_PREFIX):
            continue
        if df is None or df.empty or "원가구분" not in df.columns:
            continue

        amount_column = next(
            (c for c in MATERIAL_COST_AMOUNT_COLUMNS if c in df.columns), None
        )
        if amount_column is None:
            continue

        # 회계연도/회계월 필터
        _df = df.copy()
        if settlement_year is not None and "회계연도" in _df.columns:
            _df = _df[pd.to_numeric(_df["회계연도"], errors="coerce").eq(int(settlement_year))]
        if settlement_month is not None and "회계월" in _df.columns:
            _df = _df[pd.to_numeric(_df["회계월"], errors="coerce").eq(int(settlement_month))]
        if _df.empty:
            continue

        cost_type = _df["원가구분"].astype(str).str.strip()
        paint_rows = _df[cost_type.eq("페인트")]
        if paint_rows.empty:
            continue

        amounts = pd.to_numeric(paint_rows[amount_column], errors="coerce").fillna(0)
        total += float(amounts.sum())

    return total


def _append_material_paint_column(final_df, manufacturing_cost_sheet_dfs, settlement_year=None, settlement_month=None):
    """재료비_페인트 컬럼 추가 (도장 시간 비례 배부, 정수 단가 × 도장시간).

    분자: 재료비 시트에서 원가구분=='페인트' 행 금액 합 (회계연도/회계월 일치)
    분모: 공정별_도장 (=유효실측시간_도장) 행 전체 합
    단가 = Excel ROUND(분자 / 분모, 0) — 정수
    각 행 = 단가 × 그 행의 공정별_도장

    호출 시점: 공정별 컬럼이 이미 만들어진 후여야 한다.
    """
    final_df["재료비_페인트"] = 0

    paint_total = _aggregate_paint_amount(manufacturing_cost_sheet_dfs, settlement_year=settlement_year, settlement_month=settlement_month)
    if paint_total == 0:
        return final_df

    if "공정별_도장" not in final_df.columns:
        return final_df

    weight_sum = float(
        pd.to_numeric(final_df["공정별_도장"], errors="coerce").fillna(0).sum()
    )
    if weight_sum == 0:
        return final_df

    # 방식 B: 정수 단가 × 행 가중치 (제조경비 배부와 동일)
    _allocate_amount_by_rounded_unit_rate(
        final_df,
        "재료비_페인트",
        paint_total,
        final_df["공정별_도장"],
    )
    return final_df


def _append_material_cost_columns(
    final_df,
    manufacturing_cost_sheet_dfs,
    verification_sheets=None,
    settlement_year=None,
    settlement_month=None,
    diagnostics=None,
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
    aggregated_df = _aggregate_material_cost_by_product_id(manufacturing_cost_sheet_dfs, settlement_year=settlement_year, settlement_month=settlement_month)
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
        # 페인트는 이미 _append_material_paint_column 에서 계산된 컬럼 값 사용
        # (시트 원본과 배부 결과의 라운딩 차이 방지)
        paint_col = (
            pd.to_numeric(final_df["재료비_페인트"], errors="coerce").fillna(0)
            if "재료비_페인트" in final_df.columns
            else pd.Series(0, index=final_df.index)
        )
        paint_sum = float(paint_col.sum())
        allocation_total = total_material_cost - direct_sum - paint_sum

        if allocation_total != 0:
            non_inspection_mask = ~final_df["매출구분"].astype(str).str.strip().eq("검사매출")
            # 비율 가중치 = 재료비_직접 + 재료비_페인트 (위에서 계산한 paint_col 재사용)
            weight_series = (
                pd.to_numeric(final_df["재료비_직접"], errors="coerce").fillna(0)
                + paint_col
            )
            _allocate_amount_proportional(
                final_df,
                "재료비_배부",
                allocation_total,
                weight_series,
                non_inspection_mask,
            )
            _adjust_material_allocation_to_target(
                final_df,
                allocation_total,
                non_inspection_mask,
                weight_series,
            )

        if diagnostics is not None:
            paint_col_sum = (
                pd.to_numeric(final_df["재료비_페인트"], errors="coerce").fillna(0).sum()
                if "재료비_페인트" in final_df.columns else 0
            )
            diagnostics.append({
                "구분": "재료비",
                "컬럼": "재료비_배부",
                "배부총액(분자)": allocation_total,
                "실제배부값합": pd.to_numeric(final_df["재료비_배부"], errors="coerce").fillna(0).sum(),
                "가중치합(분모)": direct_sum + paint_col_sum,
                "단가": "",
                "비고": (
                    f"검증시트 재료비 합계 {total_material_cost:,.0f}"
                    f" - 재료비_직접 합 {direct_sum:,.0f}"
                    f" - 페인트 합 {paint_sum:,.0f}"
                    f" = 배부총액 {allocation_total:,.0f}"
                    f" | 가중치합(직접+페인트) {direct_sum + paint_col_sum:,.0f}"
                ),
            })

    # 3) 재료비_합 = 직접 + 배부 + 페인트
    paint_series = (
        pd.to_numeric(final_df["재료비_페인트"], errors="coerce").fillna(0)
        if "재료비_페인트" in final_df.columns
        else pd.Series(0, index=final_df.index)
    )
    final_df["재료비_합"] = (
        pd.to_numeric(final_df["재료비_직접"], errors="coerce").fillna(0)
        + pd.to_numeric(final_df["재료비_배부"], errors="coerce").fillna(0)
        + paint_series
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


def _aggregate_direct_expense_by_product_id(manufacturing_cost_sheet_dfs, settlement_year=None, settlement_month=None):
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

        # 회계연도/회계월 필터
        _df = df.copy()
        if settlement_year is not None and "회계연도" in _df.columns:
            _df = _df[pd.to_numeric(_df["회계연도"], errors="coerce").eq(int(settlement_year))]
        if settlement_month is not None and "회계월" in _df.columns:
            _df = _df[pd.to_numeric(_df["회계월"], errors="coerce").eq(int(settlement_month))]
        if _df.empty:
            continue

        temp = _df[["상품ID", amount_column]].copy()
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
    allocations = raw_allocations.apply(_round_half_up)

    if adjust_remainder:
        remainder = total_amount - allocations.sum()
        remainder_int = _round_half_up(remainder)
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
    """Excel ROUND(value, 0) 과 같은 스칼라 반올림."""
    return _excel_round(value, 0)



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
    # 각 행 = 가중치 × 정수단가, 행별 결과도 정수로 반올림 (0.5 올림)
    # float64 곱셈은 5.10×25=127.4999... 같은 오차가 생기므로
    # Decimal(str(weight)) × Decimal(str(unit_rate)) 로 십진수 정밀 계산 후 반올림
    from decimal import Decimal, ROUND_HALF_UP as _ROUND_HALF_UP
    _ur_dec = Decimal(str(unit_rate))
    def _decimal_mul_round(w):
        if w != w:  # NaN
            return 0
        result = Decimal(str(w)) * _ur_dec
        return int(result.quantize(Decimal("1"), rounding=_ROUND_HALF_UP))
    allocations = weights.apply(_decimal_mul_round)
    # 대상 컬럼이 int dtype 이면 float 값 대입 시 에러(pandas 2.x) → float 로 캐스팅
    final_df[target_column] = pd.to_numeric(
        final_df[target_column], errors="coerce"
    ).fillna(0).astype(float)
    final_df.loc[row_mask, target_column] = allocations.values

    return float(allocations.sum()), int(unit_rate)


def _adjust_allocation_remainder_to_first_value(
    final_df, target_column, target_total, row_mask=None, weight_series=None,
):
    """컬럼 합계가 목표 금액과 다르면 첫 배부 가능 행에 차이를 보정."""
    if row_mask is None:
        row_mask = pd.Series(True, index=final_df.index)

    final_df[target_column] = pd.to_numeric(
        final_df[target_column], errors="coerce"
    ).fillna(0).astype(float)

    values = pd.to_numeric(final_df.loc[row_mask, target_column], errors="coerce").fillna(0)
    current_total = float(values.sum())
    remainder = float(target_total) - current_total
    if abs(remainder) < 1e-9:
        return current_total, 0.0

    candidate_indices = list(values[values.ne(0)].index)
    if not candidate_indices and weight_series is not None:
        weights = pd.to_numeric(weight_series[row_mask], errors="coerce").fillna(0)
        candidate_indices = list(weights[weights.ne(0)].index)
    if not candidate_indices:
        candidate_indices = list(values.index)
    if not candidate_indices:
        return current_total, 0.0

    final_df.loc[candidate_indices[0], target_column] += remainder
    adjusted_total = float(
        pd.to_numeric(final_df.loc[row_mask, target_column], errors="coerce").fillna(0).sum()
    )
    return adjusted_total, remainder


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
        settlement_year=settlement_year,
        settlement_month=settlement_month,
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
        final_df["제조경비_직접"] = _excel_round_series(
            pd.Series(values, index=final_df.index)
        ).fillna(0)

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
            "구분": "제조경비",
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
                "구분": "제조경비",
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
            "구분": "제조경비",
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
    extra_unit_rate = 0
    extra_actual = 0.0
    extra_remainder = 0.0
    if (
        extra_allocation_total != 0
        and "rtc_일수" in final_df.columns
    ):
        extra_weight_sum = float(
            pd.to_numeric(final_df["rtc_일수"], errors="coerce").fillna(0).sum()
        )
        extra_actual, extra_unit_rate = _allocate_amount_by_rounded_unit_rate(
            final_df,
            MANUFACTURING_EXPENSE_EXTRA_ALLOCATION_COLUMN,
            extra_allocation_total,
            final_df["rtc_일수"],
        )
        extra_actual, extra_remainder = _adjust_allocation_remainder_to_first_value(
            final_df,
            MANUFACTURING_EXPENSE_EXTRA_ALLOCATION_COLUMN,
            extra_allocation_total,
            weight_series=final_df["rtc_일수"],
        )

    extra_remainder_note = ""
    if abs(extra_remainder) >= 1e-9:
        extra_remainder_note = f" / 차액 {extra_remainder:+,.0f} 첫 값 보정"

    if diagnostics is not None:
        diagnostics.append({
            "구분": "제조경비",
            "컬럼": MANUFACTURING_EXPENSE_EXTRA_ALLOCATION_COLUMN,
            "배부총액(분자)": extra_allocation_total,
            "실제배부값합": extra_actual,
            "가중치합(분모)": extra_weight_sum,
            "단가": extra_unit_rate,
            "비고": (
                f"분자합 {numerator_sum:,.0f} - 실제배부합계 {actual_sum:,.0f}"
                f" / 가중치=rtc_일수 (단가×가중치)"
                f"{extra_remainder_note}"
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


def _adjust_labor_remainder_to_first_value(
    final_df, target_column, target_total, row_mask=None, weight_series=None,
):
    """노무비 세부 컬럼 합계가 분자와 다르면 값이 있는 첫 행에 차이를 보정."""
    if row_mask is None:
        row_mask = pd.Series(True, index=final_df.index)

    values = pd.to_numeric(final_df.loc[row_mask, target_column], errors="coerce").fillna(0)
    current_total = float(values.sum())
    remainder = float(target_total) - current_total
    if abs(remainder) < 1e-9:
        return current_total, 0.0

    candidate_indices = list(values[values.ne(0)].index)
    if not candidate_indices and weight_series is not None:
        weights = pd.to_numeric(weight_series[row_mask], errors="coerce").fillna(0)
        candidate_indices = list(weights[weights.ne(0)].index)
    if not candidate_indices:
        candidate_indices = list(values.index)
    if not candidate_indices:
        return current_total, 0.0

    first_index = candidate_indices[0]
    final_df.loc[first_index, target_column] = (
        pd.to_numeric(
            pd.Series([final_df.loc[first_index, target_column]]), errors="coerce",
        ).fillna(0).iloc[0]
        + remainder
    )
    adjusted_total = float(
        pd.to_numeric(final_df.loc[row_mask, target_column], errors="coerce").fillna(0).sum()
    )
    return adjusted_total, remainder


def _append_labor_cost_columns(final_df, manufacturing_cost_sheet_dfs, diagnostics=None):
    """노무비_합계 / 전체 / RQI / 정비 / 판금 / 도장 / 선물 컬럼 추가.

    공통 (전체/RQI/정비/판금/도장):
        - 노무비 시트의 배부대상별 차변 합산 = 배부할 총액
        - 단가를 반올림한 뒤 공정별_{카테고리} 값에 곱해 행별 배부
        - 배부총액과 합계 차액은 값이 있는 첫 행에 보정

    노무비_선물 (다른 규칙):
        - 분자: 노무비 시트 배부대상=='선물' 차변 합
        - 분모/가중치: 분류1=='선물' 인 행의 rtc_일수
        - 분류1=='선물' 이 아닌 행은 0
        - 배부총액과 합계 차액은 값이 있는 첫 행에 보정

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
        weight_sum = float(
            pd.to_numeric(final_df[process_column], errors="coerce").fillna(0).sum()
        )
        actual_sum = 0.0
        labor_remainder = 0.0

        if labor_total != 0:
            actual_sum, unit_rate = _allocate_amount_by_rounded_unit_rate(
                final_df,
                column_name,
                labor_total,
                final_df[process_column],
            )
            actual_sum, labor_remainder = _adjust_labor_remainder_to_first_value(
                final_df,
                column_name,
                labor_total,
                weight_series=final_df[process_column],
            )
        else:
            unit_rate = 0

        remainder_note = ""
        if abs(labor_remainder) >= 1e-9:
            remainder_note = f" / 차액 {labor_remainder:+,.0f} 첫 값 보정"

        if diagnostics is not None:
            diagnostics.append({
                "구분": "노무비",
                "컬럼": column_name,
                "배부총액(분자)": labor_total,
                "실제배부값합": actual_sum,
                "가중치합(분모)": weight_sum,
                "단가": unit_rate,
                "비고": (
                    f"노무비 시트(배부대상={allocation_target})"
                    f" / 가중치={process_column}"
                    " / 단가×가중치"
                    f"{remainder_note}"
                ),
            })

    # 2) 노무비_선물 (분류1=='선물' 행의 rtc_일수 비율로 배부)
    gift_labor_total = float(labor_total_by_target.get(LABOR_COST_GIFT_TARGET, 0))
    gift_labor_weight_sum = 0.0
    gift_labor_actual_sum = 0.0
    gift_labor_unit_rate = 0
    gift_labor_remainder = 0.0
    if (
        LABOR_COST_GIFT_FILTER_COLUMN in final_df.columns
        and LABOR_COST_GIFT_WEIGHT_COLUMN in final_df.columns
    ):
        gift_mask = (
            final_df[LABOR_COST_GIFT_FILTER_COLUMN].astype(str).str.strip()
            .eq(LABOR_COST_GIFT_FILTER_VALUE)
        )
        if gift_mask.any():
            gift_labor_weight_sum = float(
                pd.to_numeric(
                    final_df.loc[gift_mask, LABOR_COST_GIFT_WEIGHT_COLUMN],
                    errors="coerce",
                ).fillna(0).sum()
            )
            if gift_labor_total != 0:
                gift_labor_actual_sum, gift_labor_unit_rate = _allocate_amount_by_rounded_unit_rate(
                    final_df,
                    LABOR_COST_GIFT_COLUMN,
                    gift_labor_total,
                    final_df[LABOR_COST_GIFT_WEIGHT_COLUMN],
                    row_mask=gift_mask,
                )
                gift_labor_actual_sum, gift_labor_remainder = (
                    _adjust_labor_remainder_to_first_value(
                        final_df,
                        LABOR_COST_GIFT_COLUMN,
                        gift_labor_total,
                        row_mask=gift_mask,
                        weight_series=final_df[LABOR_COST_GIFT_WEIGHT_COLUMN],
                    )
                )
            else:
                gift_labor_unit_rate = 0

    gift_remainder_note = ""
    if abs(gift_labor_remainder) >= 1e-9:
        gift_remainder_note = f" / 차액 {gift_labor_remainder:+,.0f} 첫 값 보정"

    if diagnostics is not None:
        diagnostics.append({
            "구분": "노무비",
            "컬럼": LABOR_COST_GIFT_COLUMN,
            "배부총액(분자)": gift_labor_total,
            "실제배부값합": gift_labor_actual_sum,
            "가중치합(분모)": gift_labor_weight_sum,
            "단가": gift_labor_unit_rate,
            "비고": (
                f"노무비 시트(배부대상={LABOR_COST_GIFT_TARGET})"
                f" / 분류1='{LABOR_COST_GIFT_FILTER_VALUE}' 행의 {LABOR_COST_GIFT_WEIGHT_COLUMN}"
                " / 단가×가중치"
                f"{gift_remainder_note}"
            ),
        })

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

    # groupby().sum()은 float64 누적 덧셈이라 9.10+0.88=9.979999... 같은 오차가 생길 수 있으므로
    # Decimal로 정확하게 합산한다
    from decimal import Decimal as _Decimal
    grouped = (
        df.groupby(["상품ID", "매출구분", "구분"])["측정시간(H)"]
        .apply(lambda s: float(sum(_Decimal(str(v)) for v in s)))
    )
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
    # float64 누적 덧셈은 5.10+2.30+1.20+1.38=9.9799... 같은 오차가 생기므로
    # 행별로 Decimal 덧셈해서 정확한 십진수 합을 구한다
    from decimal import Decimal as _Decimal
    def _decimal_row_sum(row):
        return float(sum(_Decimal(str(v)) for v in row))
    cols_for_total = list(PROCESS_CATEGORY_COLUMNS.keys())
    final_df[PROCESS_CATEGORY_TOTAL_COLUMN] = (
        final_df[cols_for_total]
        .apply(pd.to_numeric, errors="coerce")
        .fillna(0)
        .apply(_decimal_row_sum, axis=1)
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


def _load_master_pnl_product_id_counts(settlement_year=None, settlement_month=None):
    """코드와 같은 위치의 master_pnl.xlsx 에서 (상품ID, 판매연도, 판매월) 조합별 개수 반환.

    반환: {(상품ID, 연, 월): 개수}. 각 상품 행의 (회계연도, 회계월) 로 매칭한다.
    파일 없거나 상품ID 컬럼 없으면 {}.
    """
    import os

    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "master_pnl.xlsx"),
        "master_pnl.xlsx",
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        return {}

    try:
        df = pd.read_excel(path)
    except Exception:
        return {}

    df = _strip_columns(df)
    id_column = next(
        (c for c in ["상품ID", "상품아이디", "차량아이디", "CODE"] if c in df.columns),
        None,
    )
    if id_column is None:
        return {}

    year_column = next(
        (c for c in ["판매연도", "판매년도", "매출연도", "매출년도", "연도", "년도"]
         if c in df.columns),
        None,
    )
    month_column = next(
        (c for c in ["판매월", "판매달", "매출월", "월"] if c in df.columns),
        None,
    )

    work = pd.DataFrame()
    work["상품ID"] = df[id_column].astype(str).str.strip()
    work["연"] = pd.to_numeric(df[year_column], errors="coerce") if year_column else pd.NA
    work["월"] = pd.to_numeric(df[month_column], errors="coerce") if month_column else pd.NA
    work = work[work["상품ID"].ne("")]
    if work.empty:
        return {}

    result = {}
    grouped = work.groupby(["상품ID", "연", "월"], dropna=False).size()
    for (pid, year, month), count in grouped.items():
        year_int = int(year) if pd.notna(year) else None
        month_int = int(month) if pd.notna(month) else None
        result[(pid, year_int, month_int)] = int(count)
    return result


# ============================================================
# 누적 마스터에서 직전월 기초재고 자동 생성
# ============================================================

# 마스터의 누적합계 컬럼 → 기초재고 시트의 _전월 컬럼 매핑
_MASTER_TO_INVENTORY_COLUMN_MAP = {
    "상품매입액_합계": "상품매입액_전월",
    "취득세_합계": "취득세_전월",
    "매입수수료_합계": "매입수수료_전월",
    "폐자원공제_합계": "폐자원공제_전월",
    "초과운행_합계": "초과운행_전월",
    "차액배부_합계": "차액배부_전월",
    "페이백(반납)_합계": "페이백(반납)_전월",
    "페이백(미반납)_합계": "페이백(미반납)_전월",
    "재료비_누적합계": "재료비_전월",
    "노무비_누적합계": "노무비_전월",
    "제조경비_누적합계": "제조경비_전월",
    "매출원가_누적합계": "매출원가_전월",
}

_MASTER_TO_INVENTORY_DETAIL_COLUMNS = [
    "신번호", "구번호", "차대번호", "차종", "차명",
    "반납일자", "매입일자", "분류1", "분류2", "분류3", "분류4",
    "매입연도", "매입월",
]


def _find_latest_cost_summary_path():
    """가장 최근 cost_summary_*.xlsx 경로 반환 (없으면 None).

    빌더 파일 위치(앱 루트)를 먼저 검색하고, 그 다음 부모 폴더/현재 작업 디렉토리로 fallback.
    """
    import os
    import glob

    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    search_dirs = [here, parent, "."]
    matched = []
    for priority, d in enumerate(search_dirs):
        try:
            for p in glob.glob(os.path.join(d, "cost_summary_*.xlsx")):
                if os.path.exists(p) and os.path.getsize(p) > 0:
                    matched.append((p, priority))
        except Exception:
            pass
    if not matched:
        return None
    matched = sorted(
        matched,
        key=lambda item: (os.path.basename(item[0]), -item[1]),
        reverse=True,
    )
    return matched[0][0]


def _get_previous_master_prepaid_product_ids(settlement_year, settlement_month):
    """직전월 최종원가마스터에서 선매입 상품ID 집합 반환."""
    if settlement_year is None or settlement_month is None:
        return set()

    settle_year = int(settlement_year)
    settle_month = int(settlement_month)
    if settle_month == 1:
        prev_year, prev_month = settle_year - 1, 12
    else:
        prev_year, prev_month = settle_year, settle_month - 1

    path = _find_latest_cost_summary_path()
    if path is None:
        return set()
    try:
        master_df = pd.read_excel(path, sheet_name="최종원가마스터")
    except Exception:
        try:
            master_df = pd.read_excel(path)
        except Exception:
            return set()

    if master_df is None or master_df.empty:
        return set()
    if not {"회계연도", "회계월", "상품ID"}.issubset(master_df.columns):
        return set()

    yr = pd.to_numeric(master_df["회계연도"], errors="coerce")
    mo = pd.to_numeric(master_df["회계월"], errors="coerce")
    prev_rows = master_df[yr.eq(prev_year) & mo.eq(prev_month)].copy()
    if prev_rows.empty:
        return set()

    prepaid_column = next(
        (column for column in ("선매입여부", "선매입", "선매입 여부") if column in prev_rows.columns),
        None,
    )
    if prepaid_column is None:
        return set()

    prepaid = prev_rows[prepaid_column].astype(str).str.strip().eq("선매입")
    return set(
        prev_rows.loc[prepaid, "상품ID"]
        .dropna().astype(str).str.strip()
        .replace("", pd.NA).dropna()
    )


def _get_previous_master_cost_group_df(settlement_year, settlement_month):
    """직전월 마스터에서 사내/위탁매출의 제조원가 전월누적 조회용 DataFrame 반환."""
    if settlement_year is None or settlement_month is None:
        return None

    settle_year = int(settlement_year)
    settle_month = int(settlement_month)
    if settle_month == 1:
        prev_year, prev_month = settle_year - 1, 12
    else:
        prev_year, prev_month = settle_year, settle_month - 1

    path = _find_latest_cost_summary_path()
    if path is None:
        return None
    try:
        master_df = pd.read_excel(path, sheet_name="최종원가마스터")
    except Exception:
        try:
            master_df = pd.read_excel(path)
        except Exception:
            return None

    required = {"회계연도", "회계월", "상품ID", "매출구분"}
    if master_df is None or master_df.empty or not required.issubset(master_df.columns):
        return None

    yr = pd.to_numeric(master_df["회계연도"], errors="coerce")
    mo = pd.to_numeric(master_df["회계월"], errors="coerce")
    sales_type = master_df["매출구분"].astype(str).str.strip()
    rows = master_df[
        yr.eq(prev_year)
        & mo.eq(prev_month)
    ].copy()
    if rows.empty:
        return None

    if rows.empty:
        return None

    source_to_target = {
        "재료비_누적합계": "재료비_전월누적",
        "노무비_누적합계": "노무비_전월누적",
        "제조경비_누적합계": "제조경비_전월누적",
    }
    available_sources = [src for src in source_to_target if src in rows.columns]
    if not available_sources:
        return None

    rows["상품ID"] = rows["상품ID"].astype(str).str.strip()
    rows = rows[rows["상품ID"].ne("")]
    if rows.empty:
        return None

    # 당사/타사 컬럼이 있으면 groupby 키에 포함 (사내/위탁매출 + 당사/타사 칼럼�� 채)
    groupby_keys = ["상품ID"]
    if "당사/타사" in rows.columns:
        rows["당사/타사"] = rows["당사/타사"].astype(str).str.strip()
        groupby_keys.append("당사/타사")

    for src in available_sources:
        rows[src] = pd.to_numeric(rows[src], errors="coerce").fillna(0)

    previous_df = rows.groupby(groupby_keys, as_index=False)[available_sources].sum()
    return previous_df.rename(columns=source_to_target)


def _build_inventory_df_from_master(settlement_year, settlement_month):
    """누적 마스터에서 직전월 데이터를 추출해 기초재고 시트 형식의 df 반환.

    1) settlement_year/month 의 직전월 계산 (1월→전년 12월)
    2) 최신 cost_summary_*.xlsx 읽기 (없으면 None)
    3) 마스터에서 (회계연도, 회계월) == 직전월 행 중 기말_수량 == 1 인 행 필터
    4) 컬럼 매핑: 누적합계 → _전월
    5) {전월}월 기말여부 = 1 으로 추가

    반환: 기초재고 형식의 DataFrame (없거나 빈 결과면 None)
    """
    if settlement_year is None or settlement_month is None:
        return None

    settle_year = int(settlement_year)
    settle_month = int(settlement_month)
    if settle_month == 1:
        prev_year, prev_month = settle_year - 1, 12
    else:
        prev_year, prev_month = settle_year, settle_month - 1

    path = _find_latest_cost_summary_path()
    if path is None:
        return None
    try:
        master_df = pd.read_excel(path, sheet_name="최종원가마스터")
    except Exception:
        try:
            master_df = pd.read_excel(path)
        except Exception:
            return None

    if master_df is None or master_df.empty:
        return None

    # 회계연도/회계월 필터
    if "회계연도" not in master_df.columns or "회계월" not in master_df.columns:
        return None

    yr = pd.to_numeric(master_df["회계연도"], errors="coerce")
    mo = pd.to_numeric(master_df["회계월"], errors="coerce")
    prev_mask = yr.eq(prev_year) & mo.eq(prev_month)
    prev_rows = master_df[prev_mask].copy()
    if prev_rows.empty:
        return None

    # 기말_수량 == 1 만 추출
    if "기말_수량" in prev_rows.columns:
        prev_rows["기말_수량"] = pd.to_numeric(
            prev_rows["기말_수량"], errors="coerce"
        ).fillna(0)
        prev_rows = prev_rows[prev_rows["기말_수량"].eq(1)]
    if prev_rows.empty:
        return None

    # 매출구분 == "사내매출" 만 추출
    if "매출구분" in prev_rows.columns:
        sales_type = prev_rows["매출구분"].astype(str).str.strip()
        prev_rows = prev_rows[sales_type.eq("사내매출")]
    if prev_rows.empty:
        return None

    # 선매입여부 != "선매입" 만 추출
    # (선매입 상품은 다음 달 매입조회에 또 잡혀서 매입조회 필터에서 처리되므로
    #  여기서 가져오면 중복됨)
    if "선매입여부" in prev_rows.columns:
        presale = prev_rows["선매입여부"].astype(str).str.strip()
        prev_rows = prev_rows[~presale.eq("선매입")]
    if prev_rows.empty:
        return None

    # 기초재고 시트 형식으로 변환
    inv = pd.DataFrame()

    # 상품ID 보존
    if "상품ID" in prev_rows.columns:
        inv["상품ID"] = prev_rows["상품ID"].astype(str).str.strip()
    else:
        return None

    # 1번 원가대상 상세 컬럼 보존: 전월 마스터의 신번호~매입월 값을 그대로 싣는다.
    for column in _MASTER_TO_INVENTORY_DETAIL_COLUMNS:
        if column in prev_rows.columns:
            inv[column] = prev_rows[column].values

    # 컬럼 매핑 적용
    for master_col, inventory_col in _MASTER_TO_INVENTORY_COLUMN_MAP.items():
        if master_col in prev_rows.columns:
            inv[inventory_col] = pd.to_numeric(
                prev_rows[master_col], errors="coerce"
            ).fillna(0)
        else:
            inv[inventory_col] = 0

    # {전월}월 기말여부 = 1 (빌더가 이 컬럼으로 전월 기말 행 필터)
    inv[f"{prev_month}월 기말여부"] = 1

    result = inv.reset_index(drop=True)
    result.attrs["_from_master_inventory"] = True
    return result


def _build_consignment_outbound_counts(consignment_ledger_df, settlement_month):
    """위탁수불부에서 상품ID별 출고 개수 반환.

    출고 여부는 출고상태 컬럼값이 위탁판매/위탁매입/위탁취소 중 하나인지로 판단한다.
    반환: {상품ID: 개수}
    """
    if consignment_ledger_df is None or consignment_ledger_df.empty:
        return {}

    df = _strip_columns(consignment_ledger_df).copy()
    if "상품ID" not in df.columns or "출고상태" not in df.columns:
        return {}

    df["상품ID"] = df["상품ID"].astype(str).str.strip()
    outbound_values = {"위탁판매", "위탁매입", "위탁취소"}
    matched = df[
        df["출고상태"].astype(str).str.strip().isin(outbound_values)
        & df["상품ID"].ne("")
    ]
    if matched.empty:
        return {}

    return matched["상품ID"].value_counts().to_dict()

def _build_consignment_outbound_status_map(consignment_ledger_df):
    """위탁수불부에서 출고상태값(위탁판매/위탁매입/위탁취소)이 있는 행의 상품ID별 출고상태 값 반환.

    출고여부 컬럼 없이 출고상태 컬럼 값 자체로 출고 여부를 판단한다.
    반환: {상품ID: 출고상태값}. 같은 상품ID 가 여러 건이면 마지막 값.
    """
    if consignment_ledger_df is None or consignment_ledger_df.empty:
        return {}

    df = _strip_columns(consignment_ledger_df).copy()
    if "상품ID" not in df.columns or "출고상태" not in df.columns:
        return {}

    df["상품ID"] = df["상품ID"].astype(str).str.strip()
    outbound_values = {"위탁판매", "위탁매입", "위탁취소"}
    matched = df[
        df["출고상태"].astype(str).str.strip().isin(outbound_values)
        & df["상품ID"].ne("")
    ]
    if matched.empty:
        return {}

    result = {}
    for _, row in matched.iterrows():
        result[row["상품ID"]] = row["출고상태"]
    return result

def _append_inventory_quantity_amount_columns(
    final_df, inventory_df, purchase_cost_sheet_dfs, settlement_month,
    consignment_ledger_df=None, settlement_year=None,
):
    """수량/금액 묶음 + 출고/기말 컬럼 부여.

    수량(기존 플래그 컬럼 재사용):
        기초_수량 = 기초재고, 정상입고_수량 = 정상입고, 타처입고_수량 = 타처입고
    금액:
        기초_금액: 기초_수량==1 이면 기초재고 시트의 같은 상품ID 매출원가_전월
        정상입고_금액: 당사차량이면 매입원가_당월, 아니면 0
        타처입고_금액: 당사차량이면 상품원장(원가구분=타처입고) 같은 상품ID 금액합, 아니면 0
        제조원가: 제조원가_당월 과 동일
    출고:
        자산출고_수량: 당사차량 & 상품원장(원가구분=자산출고) 같은 상품ID 개수
        자산출고_금액: 자산출고_수량==1 이면 (기초+정상입고+타처입고+제조원가) 금액합
        기타출고_수량/금액: 자산출고와 동일 패턴(원가구분=기타출고)
    기말:
        기말_수량 = 기초+정상입고+타처입고 - 정상출고 - 자산출고 - 기타출고 (수량)
        기말_금액 = 당사차량만 (기초+정상입고+타처입고+제조원가 - 정상출고 - 자산출고 - 기타출고) 금액
    정상출고_수량/금액: 추후 정의 (지금은 0 으로 둠)
    """
    n = len(final_df)

    def zero():
        return pd.Series([0.0] * n, index=final_df.index)

    product_id_series = final_df["상품ID"].astype(str).str.strip()
    own_vehicle_mask = (
        final_df["당사/타사"].astype(str).str.strip().eq("당사차량")
        if "당사/타사" in final_df.columns
        else pd.Series([False] * n, index=final_df.index)
    )

    # ----- 수량 (기존 플래그 컬럼 그대로 숫자화) -----
    qty_base = (
        pd.to_numeric(final_df["기초재고"], errors="coerce").fillna(0)
        if "기초재고" in final_df.columns else zero()
    )
    qty_normal = (
        pd.to_numeric(final_df["정상입고"], errors="coerce").fillna(0)
        if "정상입고" in final_df.columns else zero()
    )
    qty_transfer = (
        pd.to_numeric(final_df["타처입고"], errors="coerce").fillna(0)
        if "타처입고" in final_df.columns else zero()
    )

    final_df["기초_수량"] = qty_base
    final_df["정상입고_수량"] = qty_normal
    final_df["타처입고_수량"] = qty_transfer

    # ----- 기초_금액: 기초_수량==1 이면 기초재고 시트의 매출원가_전월 -----
    base_amount = zero()
    if (
        inventory_df is not None
        and not inventory_df.empty
        and "상품ID" in inventory_df.columns
    ):
        inv = _strip_columns(inventory_df).copy()
        if "매출원가_전월" in inv.columns:
            inv["상품ID"] = inv["상품ID"].astype(str).str.strip()
            inv["매출원가_전월"] = pd.to_numeric(
                inv["매출원가_전월"], errors="coerce"
            ).fillna(0)
            inv = inv[inv["상품ID"].ne("")]
            _is_from_master = bool(getattr(inventory_df, "attrs", {}).get("_from_master_inventory"))
            if _is_from_master and "매출구분" in final_df.columns:
                # 전월 마스터 파일에서 직접 최신파일 쵬출 후 전체 매출구분 대상으로 상품ID+매출구분 SUMIFS
                _path = _find_latest_cost_summary_path()
                _master_raw = None
                if _path is not None:
                    try:
                        _master_raw = pd.read_excel(_path, sheet_name="최종원가마스터")
                    except Exception:
                        try:
                            _master_raw = pd.read_excel(_path)
                        except Exception:
                            pass
                if _master_raw is not None and not _master_raw.empty:
                    _settle_year = int(settlement_year) if settlement_year else None
                    _settle_month = int(settlement_month) if settlement_month else None
                    if _settle_month == 1:
                        _prev_year, _prev_month = _settle_year - 1, 12
                    else:
                        _prev_year, _prev_month = _settle_year, _settle_month - 1
                    _yr = pd.to_numeric(_master_raw.get("회계연도"), errors="coerce")
                    _mo = pd.to_numeric(_master_raw.get("회계월"), errors="coerce")
                    _mrows = _master_raw[_yr.eq(_prev_year) & _mo.eq(_prev_month)].copy()
                    if not _mrows.empty and "기말_금액" in _mrows.columns:
                        _mrows["상품ID"] = _mrows["상품ID"].astype(str).str.strip()
                        _mrows["매출구분"] = _mrows["매출구분"].astype(str).str.strip()
                        _mrows["기말_금액"] = pd.to_numeric(_mrows["기말_금액"], errors="coerce").fillna(0)
                        _base_df = _mrows.groupby(["상품ID", "매출구분"])["기말_금액"].sum().reset_index()
                        _base_df["_key"] = _base_df["상품ID"] + "|" + _base_df["매출구분"]
                        _lookup = (product_id_series + "|" + final_df["매출구분"].astype(str).str.strip())
                        _base_map = _base_df.set_index("_key")["기말_금액"].to_dict()
                        base_amount = _lookup.map(_base_map).fillna(0).astype(float)
            else:
                base_cost_map = inv.groupby("상품ID")["매출원가_전월"].sum().to_dict()
                base_amount = product_id_series.map(base_cost_map).fillna(0).astype(float)
    base_amount = base_amount.where(qty_base.eq(1), 0)
    final_df["기초_금액"] = base_amount

    # ----- 정상입고_금액: 당사차량이면 매입원가_당월(내부명 '매입원가') -----
    purchase_current = (
        pd.to_numeric(final_df["매입원가"], errors="coerce").fillna(0)
        if "매입원가" in final_df.columns else zero()
    )
    final_df["정상입고_금액"] = purchase_current.where(own_vehicle_mask, 0)

    # ----- 타처입고_금액: 당사차량이면 상품원장(원가구분=타처입고) 금액합 -----
    transfer_ledger = _aggregate_ledger_by_cost_type(purchase_cost_sheet_dfs, "타처입고")
    transfer_amount_map = {pid: v["amount"] for pid, v in transfer_ledger.items()}
    transfer_amount = product_id_series.map(transfer_amount_map).fillna(0).astype(float)
    final_df["타처입고_금액"] = transfer_amount.where(own_vehicle_mask, 0)

    # ----- 제조원가 = 제조원가_당월 -----
    mfg_cost = (
        pd.to_numeric(final_df["제조원가_당월"], errors="coerce").fillna(0)
        if "제조원가_당월" in final_df.columns else zero()
    )
    final_df["제조원가"] = mfg_cost

    # ----- 입고 금액 합 (기초+정상입고+타처입고+제조원가) -----
    inbound_amount_sum = (
        final_df["기초_금액"]
        + final_df["정상입고_금액"]
        + final_df["타처입고_금액"]
        + final_df["제조원가"]
    )

    # ----- 자산출고 -----
    asset_ledger = _aggregate_ledger_by_cost_type(purchase_cost_sheet_dfs, "자산출고")
    asset_count_map = {pid: v["count"] for pid, v in asset_ledger.items()}
    asset_qty = product_id_series.map(asset_count_map).fillna(0).astype(float)
    asset_qty = asset_qty.where(own_vehicle_mask, 0)
    final_df["자산출고_수량"] = asset_qty
    final_df["자산출고_금액"] = inbound_amount_sum.where(asset_qty.eq(1), 0)

    # ----- 기타출고 -----
    etc_ledger = _aggregate_ledger_by_cost_type(purchase_cost_sheet_dfs, "기타출고")
    etc_count_map = {pid: v["count"] for pid, v in etc_ledger.items()}
    etc_qty = product_id_series.map(etc_count_map).fillna(0).astype(float)
    etc_qty = etc_qty.where(own_vehicle_mask, 0)
    final_df["기타출고_수량"] = etc_qty
    final_df["기타출고_금액"] = inbound_amount_sum.where(etc_qty.eq(1), 0)

    # ----- 정상출고 -----
    # 정상출고_수량:
    #   당사차량: master_pnl.xlsx 에서 같은 상품ID 개수
    #   위탁매출: 위탁수불부에서 같은 상품ID 의 출고==1 개수
    sales_type_series = (
        final_df["매출구분"].astype(str).str.strip()
        if "매출구분" in final_df.columns
        else pd.Series([""] * n, index=final_df.index)
    )

    # (a) master_pnl 개수: 각 행의 (상품ID, 회계연도, 회계월) 로 매칭
    master_count_map = _load_master_pnl_product_id_counts()
    if "회계연도" in final_df.columns:
        acct_year = pd.to_numeric(final_df["회계연도"], errors="coerce")
    else:
        acct_year = pd.Series([settlement_year] * n, index=final_df.index)
    if "회계월" in final_df.columns:
        acct_month = pd.to_numeric(final_df["회계월"], errors="coerce")
    else:
        acct_month = pd.Series([settlement_month] * n, index=final_df.index)

    def _master_lookup(pid, yr, mo):
        yr_int = int(yr) if pd.notna(yr) else None
        mo_int = int(mo) if pd.notna(mo) else None
        return master_count_map.get((str(pid).strip(), yr_int, mo_int), 0)

    master_qty = pd.Series(
        [
            _master_lookup(pid, yr, mo)
            for pid, yr, mo in zip(product_id_series, acct_year, acct_month)
        ],
        index=final_df.index,
        dtype=float,
    )

    # (b) 위탁수불부 출고==1 개수
    consignment_count_map = _build_consignment_outbound_counts(
        consignment_ledger_df, settlement_month,
    )
    consignment_qty = product_id_series.map(consignment_count_map).fillna(0).astype(float)

    consignment_sales_mask = sales_type_series.eq("위탁매출")
    normal_out_qty = pd.Series([0.0] * n, index=final_df.index)
    # 당사차량 → master_pnl 개수
    normal_out_qty = normal_out_qty.where(~own_vehicle_mask, master_qty)
    # 위탁매출 → 위탁수불부 출고 개수 (당사차량이 아닌 위탁매출에 적용)
    normal_out_qty = normal_out_qty.where(
        ~(consignment_sales_mask & ~own_vehicle_mask), consignment_qty
    )
    final_df["정상출고_수량"] = normal_out_qty

    # 정상출고_금액: 정상출고_수량==1 이면 입고금액합
    final_df["정상출고_금액"] = inbound_amount_sum.where(normal_out_qty.eq(1), 0)

    normal_out_qty = pd.to_numeric(final_df["정상출고_수량"], errors="coerce").fillna(0)
    normal_out_amount = pd.to_numeric(final_df["정상출고_금액"], errors="coerce").fillna(0)

    # ----- 기말_수량 = 기초+정상입고+타처입고 - 정상출고 - 자산출고 - 기타출고 -----
    final_df["기말_수량"] = (
        qty_base + qty_normal + qty_transfer
        - normal_out_qty - asset_qty - etc_qty
    )

    # ----- 기말_금액 = 기초_금액 + 입고금액합 - 정상출고 - 자산출고 - 기타출고 -----
    ending_amount = (
        inbound_amount_sum
        - normal_out_amount
        - final_df["자산출고_금액"]
        - final_df["기타출고_금액"]
    )
    final_df["기말_금액"] = ending_amount

    # 위탁출고구분: 위탁수불부 출고여부==1 인 행의 출고상태 값 (상품ID 매칭)
    status_map = _build_consignment_outbound_status_map(consignment_ledger_df)
    final_df["위탁출고구분"] = product_id_series.map(status_map)
    final_df["위탁출고구분"] = final_df["위탁출고구분"].apply(
        lambda v: "" if pd.isna(v) else str(v)
    )

    return final_df


def _append_cost_group_cumulative_columns(
    final_df, inventory_df, settlement_month, settlement_year=None,
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
    from_master_inventory = bool(
        getattr(inventory_df, "attrs", {}).get("_from_master_inventory")
    )

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

    if from_master_inventory:
        previous_master_df = _get_previous_master_cost_group_df(
            settlement_year, settlement_month,
        )
        if previous_master_df is not None and not previous_master_df.empty:
            target_columns = [
                column for column in [
                    "재료비_전월누적", "노무비_전월누적", "제조경비_전월누적",
                ]
                if column in previous_master_df.columns
            ]
            if target_columns:
                final_df = final_df.drop(columns=target_columns)
                # 상품ID + 매출구분 + 당사/타사 로 merge
                # (상품ID만 쓰면 당사/타사가 다른 행에 엉뚱항 값이 붙는 문제 방지)
                _merge_keys = ["상품ID"]
                if "당사/타사" in final_df.columns and "당사/타사" in previous_master_df.columns:
                    _merge_keys.append("당사/타사")
                _left = final_df.copy()
                _right = previous_master_df[[*_merge_keys, *target_columns]].copy()
                for _k in _merge_keys:
                    _left[_k] = _left[_k].astype(str).str.strip()
                    _right[_k] = _right[_k].astype(str).str.strip()
                final_df = _left.merge(_right, on=_merge_keys, how="left")
                for column in target_columns:
                    final_df[column] = pd.to_numeric(
                        final_df[column], errors="coerce"
                    ).fillna(0)

    # 3) 누적합계 = 전월누적 + 당월
    for group_name, _ in group_specs:
        final_df[f"{group_name}_누적합계"] = (
            pd.to_numeric(final_df[f"{group_name}_전월누적"], errors="coerce").fillna(0)
            + pd.to_numeric(final_df[f"{group_name}_당월"], errors="coerce").fillna(0)
        )

    # 3-1) 일반 기초재고는 당사차량만 전월누적 유지.
    #      전월 마스터 자동 기초재고는 사내매출/위탁매출의 전월누적을 유지.
    if "당사/타사" in final_df.columns or "매출구분" in final_df.columns:
        if from_master_inventory and "매출구분" in final_df.columns:
            keep_previous_rows = (
                final_df["매출구분"].astype(str).str.strip()
                .isin(["사내매출", "위탁매출"])
            )
        elif "당사/타사" in final_df.columns:
            keep_previous_rows = final_df["당사/타사"].astype(str).str.strip().eq("당사차량")
        else:
            keep_previous_rows = pd.Series(True, index=final_df.index)

        reset_previous_rows = ~keep_previous_rows
        if reset_previous_rows.any():
            zero_columns = [
                f"{group_name}_전월누적"
                for group_name, _ in group_specs
            ]
            existing = [c for c in zero_columns if c in final_df.columns]
            for col in existing:
                final_df[col] = pd.to_numeric(
                    final_df[col], errors="coerce"
                ).fillna(0).astype(float)
            final_df.loc[reset_previous_rows, existing] = 0
            # 전월누적이 바뀌었으니 누적합계 재계산 (당월 + 새 전월누적)
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
    purchase_cost_sheet_dfs=None,
    consignment_ledger_df=None,
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
    final_df = _append_cost_driver_columns(
        final_df, cost_driver_dfs, settlement_year, settlement_month,
    )
    final_df = _append_process_category_columns(final_df, combined_cost_driver_df)
    # 재료비_페인트: 공정별_도장 이 만들어진 후 호출 (도장 시간 비례)
    final_df = _append_material_paint_column(final_df, manufacturing_cost_sheet_dfs, settlement_year=settlement_year, settlement_month=settlement_month)
    material_allocation_diagnostics = []
    cost_allocation_diagnostics = []
    # 재료비_배부 내역 (페인트 이후에 diagnostics 기록)
    final_df = _append_material_cost_columns(
        final_df, manufacturing_cost_sheet_dfs,
        verification_sheets=verification_sheets,
        settlement_year=settlement_year,
        settlement_month=settlement_month,
        diagnostics=material_allocation_diagnostics,
    )
    # 노무비는 공정별 컬럼이 채워진 후에 계산해야 함 (분모로 공정별_*_합 사용)
    final_df = _append_labor_cost_columns(
        final_df,
        manufacturing_cost_sheet_dfs,
        diagnostics=cost_allocation_diagnostics,
    )
    final_df = _append_manufacturing_expense_columns(
        final_df,
        manufacturing_cost_sheet_dfs,
        verification_sheets=verification_sheets,
        settlement_year=settlement_year,
        settlement_month=settlement_month,
        diagnostics=cost_allocation_diagnostics,
    )
    # 진단 내역을 결과 DataFrame 의 attrs 에 저장 (UI 에서 표시용)
    final_df.attrs["재료비_배부내역"] = material_allocation_diagnostics
    final_df.attrs["제조경비_배부내역"] = cost_allocation_diagnostics
    # df.attrs 가 이후 연산/캐시로 사라질 수 있으므로 모듈 레벨에도 백업
    global _LAST_MANUFACTURING_EXPENSE_DIAGNOSTICS, _LAST_MATERIAL_ALLOCATION_DIAGNOSTICS
    _LAST_MANUFACTURING_EXPENSE_DIAGNOSTICS = cost_allocation_diagnostics
    _LAST_MATERIAL_ALLOCATION_DIAGNOSTICS = material_allocation_diagnostics

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
        final_df, inventory_df, settlement_month, settlement_year=settlement_year,
    )

    # 수량/금액 묶음 + 출고/기말 (제조원가_당월 이 만들어진 후)
    final_df = _append_inventory_quantity_amount_columns(
        final_df, inventory_df, purchase_cost_sheet_dfs, settlement_month,
        consignment_ledger_df=consignment_ledger_df,
        settlement_year=settlement_year,
    )

    # 페이백 합계 = 페이백(반납) + 페이백(미반납) (합계/전월/당월 각각)
    for suffix_internal, suffix_label in (("_합계", "_합계"), ("_전월", "_전월"), ("", "_당월")):
        return_col = f"{PAYBACK_RETURN_COLUMN}{suffix_internal}"      # 페이백(반납)...
        unreturned_col = f"{PAYBACK_UNRETURNED_COLUMN}{suffix_internal}"  # 페이백(미반납)...
        target_col = f"페이백{suffix_label}"
        final_df[target_col] = (
            pd.to_numeric(final_df.get(return_col, 0), errors="coerce").fillna(0)
            + pd.to_numeric(final_df.get(unreturned_col, 0), errors="coerce").fillna(0)
        )

    # 매출원가 = 매입원가 + 제조원가 (누적합계/전월누적/당월 각각)
    # 매입원가는 내부명: 합계=매입원가_합계, 전월누적=매입원가_전월, 당월=매입원가
    purchase_map = {
        "누적합계": f"{TOTAL_PURCHASE_COST_COLUMN}_합계",
        "전월누적": f"{TOTAL_PURCHASE_COST_COLUMN}_전월",
        "당월": TOTAL_PURCHASE_COST_COLUMN,
    }
    for kind in ("누적합계", "전월누적", "당월"):
        purchase_col = purchase_map[kind]
        mfg_col = f"제조원가_{kind}"
        final_df[f"매출원가_{kind}"] = (
            pd.to_numeric(final_df.get(purchase_col, 0), errors="coerce").fillna(0)
            + pd.to_numeric(final_df.get(mfg_col, 0), errors="coerce").fillna(0)
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
        # 페이백(반납) 묶음 앞에 통합 페이백 합계/전월/당월 삽입
        if column == PAYBACK_RETURN_COLUMN:
            tail_columns.extend(["페이백_합계", "페이백_전월", "페이백_당월"])
        tail_columns.extend([f"{column}_합계", f"{column}_전월", column])
    # 초과운행_당월(EXCESS_DRIVING_COLUMN) 뒤: 재료비/노무비/제조경비 누적 묶음
    for group_name in ("재료비", "노무비", "제조경비"):
        tail_columns.extend([
            f"{group_name}_누적합계",
            f"{group_name}_전월누적",
            f"{group_name}_당월",
        ])
    tail_columns.extend(["rtc_일수", "sm_일수"])
    tail_columns.extend([PROCESS_CATEGORY_TOTAL_COLUMN])
    tail_columns.extend(PROCESS_CATEGORY_COLUMNS.keys())
    # 당월_재료비(재료비_합/직접/배부)는 유효실측시간_도장(공정별_도장) 뒤에 위치
    tail_columns.extend(["재료비_합", "재료비_직접", "재료비_페인트", "재료비_배부"])
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

    # 수량/금액 묶음 컬럼: 원본 기초재고/정상입고/타처입고 위치에 배치하고 원본은 제거
    inventory_block = [
        "기초_수량", "기초_금액",
        "정상입고_수량", "정상입고_금액",
        "타처입고_수량", "타처입고_금액",
        "제조원가",
        "정상출고_수량", "정상출고_금액",
        "자산출고_수량", "자산출고_금액",
        "기타출고_수량", "기타출고_금액",
        "기말_수량", "기말_금액",
        # 기말_금액 뒤: 위탁출고구분, 그 다음 매출원가
        "위탁출고구분",
        "매출원가_누적합계", "매출원가_전월누적", "매출원가_당월",
    ]
    inventory_block = [c for c in inventory_block if c in final_df.columns]
    original_qty_columns = ["기초재고", "정상입고", "타처입고"]

    # base_columns 에서 원본 수량 컬럼 제거 + 새 묶음/중복 제거
    base_columns = [
        c for c in base_columns
        if c not in original_qty_columns and c not in inventory_block
    ]
    # 원본 '매입월' 뒤에 inventory_block 삽입 (없으면 당사/타사 뒤, 그것도 없으면 base 끝)
    insert_pos = len(base_columns)
    if "매입월" in base_columns:
        insert_pos = base_columns.index("매입월") + 1
    elif "당사/타사" in base_columns:
        insert_pos = base_columns.index("당사/타사") + 1
    base_columns = (
        base_columns[:insert_pos] + inventory_block + base_columns[insert_pos:]
    )

    ordered = base_columns + tail_columns
    ordered = [c for c in ordered if c in final_df.columns]

    # 상품ID 를 선매입여부 뒤로 이동
    if "상품ID" in ordered and "선매입여부" in ordered:
        ordered.remove("상품ID")
        insert_at = ordered.index("선매입여부") + 1
        ordered.insert(insert_at, "상품ID")

    final_df = final_df[ordered]

    # 최종 출력 컬럼명으로 변경 (계산은 내부 이름으로 끝났고 표시용만 rename)
    _saved_attrs = dict(final_df.attrs)
    final_df = final_df.rename(columns=_build_output_column_rename_map())
    final_df.attrs.update(_saved_attrs)

    # 최종 정렬
    final_df = _sort_final_cost_df(final_df)
    return final_df


def _sort_final_cost_df(final_df):
    """최종 원가 정렬.

    1) 매출구분: 사내매출 > 위탁매출 > 검사매출 > 정비매출
    2) 기초_수량==1 > 정상입고_수량==1 > 타처입고_수량==1 (해당 수량이 1인 순서)
    3) 당사차량 → 매입일자(계산서일자) 오름차순 / 타사차량 → 반납일자 오름차순
    """
    if final_df.empty:
        return final_df

    df = final_df.copy()

    # 1) 매출구분 우선순위
    sales_order = {"사내매출": 0, "위탁매출": 1, "검사매출": 2, "정비매출": 3}
    sales_key = (
        df["매출구분"].astype(str).str.strip().map(sales_order).fillna(99)
        if "매출구분" in df.columns
        else pd.Series([99] * len(df), index=df.index)
    )

    # 2) 수량 우선순위: 기초==1 →0, 정상입고==1 →1, 타처입고==1 →2, 그 외 →3
    def qty_rank(row):
        if "기초_수량" in df.columns and pd.to_numeric(pd.Series([row.get("기초_수량", 0)]), errors="coerce").fillna(0).iloc[0] == 1:
            return 0
        if "정상입고_수량" in df.columns and pd.to_numeric(pd.Series([row.get("정상입고_수량", 0)]), errors="coerce").fillna(0).iloc[0] == 1:
            return 1
        if "타처입고_수량" in df.columns and pd.to_numeric(pd.Series([row.get("타처입고_수량", 0)]), errors="coerce").fillna(0).iloc[0] == 1:
            return 2
        return 3

    base_q = pd.to_numeric(df.get("기초_수량", 0), errors="coerce").fillna(0)
    normal_q = pd.to_numeric(df.get("정상입고_수량", 0), errors="coerce").fillna(0)
    transfer_q = pd.to_numeric(df.get("타처입고_수량", 0), errors="coerce").fillna(0)
    qty_key = pd.Series(3, index=df.index)
    qty_key = qty_key.mask(transfer_q.eq(1), 2)
    qty_key = qty_key.mask(normal_q.eq(1), 1)
    qty_key = qty_key.mask(base_q.eq(1), 0)

    # 3) 날짜 키: 당사차량 → 매입일자, 타사차량 → 반납일자
    own_mask = (
        df["당사/타사"].astype(str).str.strip().eq("당사차량")
        if "당사/타사" in df.columns
        else pd.Series([False] * len(df), index=df.index)
    )
    purchase_date = (
        pd.to_datetime(df["매입일자"], errors="coerce")
        if "매입일자" in df.columns
        else pd.Series([pd.NaT] * len(df), index=df.index)
    )
    return_date = (
        pd.to_datetime(df["반납일자"], errors="coerce")
        if "반납일자" in df.columns
        else pd.Series([pd.NaT] * len(df), index=df.index)
    )
    date_key = purchase_date.where(own_mask, return_date)

    df["_sales_key"] = sales_key.values
    df["_qty_key"] = qty_key.values
    df["_date_key"] = date_key.values

    df = df.sort_values(
        by=["_sales_key", "_qty_key", "_date_key"],
        ascending=[True, True, True],
        na_position="last",
        kind="stable",
    ).drop(columns=["_sales_key", "_qty_key", "_date_key"]).reset_index(drop=True)

    df.attrs.update(final_df.attrs)
    return df


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
    rename_map["재료비_페인트"] = "당월_재료비_페인트"
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
    consignment_ledger_df=None,
):
    final_df = product_id_df.copy()

    for column in FINAL_COST_AMOUNT_COLUMNS:
        final_df[column] = 0
    final_df[DIFFERENCE_ALLOCATION_COLUMN] = 0

    if final_df.empty:
        return final_df

    # 기초재고가 비어 있으면 누적 마스터에서 직전월 자동 생성
    if inventory_df is None or (
        isinstance(inventory_df, pd.DataFrame) and inventory_df.empty
    ):
        auto_inventory = _build_inventory_df_from_master(
            settlement_year, settlement_month,
        )
        if auto_inventory is not None and not auto_inventory.empty:
            inventory_df = auto_inventory

    reorder_kwargs = dict(
        manufacturing_cost_sheet_dfs=manufacturing_cost_sheet_dfs,
        verification_sheets=verification_sheets,
        settlement_year=settlement_year,
        settlement_month=settlement_month,
        cost_driver_dfs=cost_driver_dfs,
        combined_cost_driver_df=combined_cost_driver_df,
        inventory_df=inventory_df,
        purchase_cost_sheet_dfs=purchase_cost_sheet_dfs,
        consignment_ledger_df=consignment_ledger_df,
    )

    # 매입원가 시트가 없으면 전월 컬럼만 채우고 종료
    if not purchase_cost_sheet_dfs:
        final_df = _append_previous_month_cost_columns(final_df, inventory_df, settlement_month)
        return _reorder_final_columns(final_df, **reorder_kwargs)

    # 1) 상품원장에서 집계
    cost_totals, transfer_in_map, purchase_amount_df = (
        _extract_product_ledger_aggregates(purchase_cost_sheet_dfs, settlement_year=settlement_year, settlement_month=settlement_month)
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
        settlement_year=settlement_year,
        settlement_month=settlement_month,
    )
    final_df = _apply_aggregated_amount(final_df, waste_df, WASTE_RESOURCE_COLUMN)

    # 3) 페이백(반납) 시트
    payback_df = _aggregate_sheet_amounts_by_product_id(
        purchase_cost_sheet_dfs,
        sheet_prefix="페이백",
        amount_column_candidates=PAYBACK_RETURN_AMOUNT_COLUMNS,
        output_column=PAYBACK_RETURN_COLUMN,
        apply_exclusion=False,
        settlement_year=settlement_year,
        settlement_month=settlement_month,
    )
    final_df = _apply_aggregated_amount(final_df, payback_df, PAYBACK_RETURN_COLUMN)

    # 숫자화
    for column in FINAL_COST_AMOUNT_COLUMNS:
        final_df[column] = pd.to_numeric(final_df[column], errors="coerce").fillna(0)
    final_df[PAYBACK_RETURN_COLUMN] = (
        _excel_round_series(final_df[PAYBACK_RETURN_COLUMN]).fillna(0)
    )

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