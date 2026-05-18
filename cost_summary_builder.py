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


def _reorder_final_columns(final_df):
    """매입원가 + 항목별 [합계, 전월, 당월] 컬럼을 뒤로 묶음.

    배치 순서:
        ... 일반 컬럼 ...
        매입원가_합계, 매입원가_전월, 매입원가,
        상품매입액_합계, 상품매입액_전월, 상품매입액,
        취득세_합계, 취득세_전월, 취득세,
        ... (이하 FINAL_COST_MONTHLY_COLUMNS 순서대로)
    """
    final_df = _append_total_purchase_cost_columns(final_df)

    tail_columns = [
        f"{TOTAL_PURCHASE_COST_COLUMN}_합계",
        f"{TOTAL_PURCHASE_COST_COLUMN}_전월",
        TOTAL_PURCHASE_COST_COLUMN,
    ]
    for column in FINAL_COST_MONTHLY_COLUMNS:
        tail_columns.extend([f"{column}_합계", f"{column}_전월", column])
    tail_columns = [c for c in tail_columns if c in final_df.columns]
    base_columns = [c for c in final_df.columns if c not in tail_columns]
    return final_df[base_columns + tail_columns]


# ============================================================
# 메인 빌더
# ============================================================

def build_final_cost_df(
    product_id_df, purchase_cost_sheet_dfs, inventory_df=None, settlement_month=None,
):
    final_df = product_id_df.copy()

    for column in FINAL_COST_AMOUNT_COLUMNS:
        final_df[column] = 0
    final_df[DIFFERENCE_ALLOCATION_COLUMN] = 0

    if final_df.empty:
        return final_df

    # 매입원가 시트가 없으면 전월 컬럼만 채우고 종료
    if not purchase_cost_sheet_dfs:
        final_df = _append_previous_month_cost_columns(final_df, inventory_df, settlement_month)
        return _reorder_final_columns(final_df)

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

    return _reorder_final_columns(final_df)