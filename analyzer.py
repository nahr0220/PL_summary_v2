import pandas as pd
import numpy as np
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

def distribute_indirect_cost(df, merged_df, category_name, col_name, target_mask=None, use_month_match=True):
    df[col_name] = 0
    df[f"{col_name}_직"] = 0
    df[f"{col_name}_간"] = 0

    # 직접비 매칭 (기존 그대로)
    if use_month_match:
        cond = ((merged_df["계정명"] == category_name) & (merged_df["판매월일치여부"] == "TRUE"))
    else:
        cond = (merged_df["계정명"] == category_name)

    direct_map = merged_df[cond].groupby("상품ID")["대변"].sum()
    df[f"{col_name}_직"] = df["상품ID"].map(direct_map).fillna(0)

    # 간접비만 월별로 계산 (df 기준으로 루프, merged_df 회계월로 합계)
    for month, month_idx in df.groupby("판매월").groups.items():
        month_mask = df.index.isin(month_idx)

        total_fee = merged_df.loc[
            (merged_df["계정명"] == category_name) & (merged_df["회계월"] == month),
            "대변"
        ].sum()

        indirect_total = total_fee - df.loc[month_mask, f"{col_name}_직"].sum()

        if target_mask is None:
            mask = month_mask & (df[f"{col_name}_직"] > 0)
        else:
            mask = month_mask & target_mask

        n = mask.sum()
        if n > 0 and indirect_total != 0:
            base_val = round(indirect_total / n)
            df.loc[mask, f"{col_name}_간"] = base_val
            diff = indirect_total - df.loc[mask, f"{col_name}_간"].sum()
            df.loc[df.index[mask][0], f"{col_name}_간"] += diff

    df[col_name] = df[f"{col_name}_직"] + df[f"{col_name}_간"]
    return df

def build_final_report(base_df, merged_df):
    col = ['매입가', '매입원가', '매입부가세', '폐자원공제액', '상품화비용', '판매가', '판매원가', '판매부가세', '매출액', 
           '매출이익', '매도비', '낙찰수수료', '매도비/낙찰수수료', '연장보증료', '찾아서', '추가상품', '성능보험료', '매출이익(목표기준)', 
           '위탁매입수수료', '위탁판매수수료', '위탁수수료', '매출총이익', '가치보장서비스', '판매목표가', '판매목표가차액', '지점판매가', 
           '지점판매가차액', '알선수수료', '임직원할인율', '임직원할인금액', '현금', '카드', '할부', '리스', '금융구분', '차량ID', '도/소매구분', '고객타입', '사업자유형', '업태', '업종']
    
    base_df = base_df.drop(columns=[c for c in col if c in base_df.columns])
    final_df = base_df.copy()

    final_df["상품/위탁"] = np.where(final_df["매입유형1"] == "위탁", "위탁", "상품")
    final_df["소/도매"] = np.where(final_df["판매지점"].str.contains("옥션", na=False),"도매", "소매")
    final_df["매출합계"] = 0

    cond_sales = ((merged_df["계정명"] == "상품매출(자동차)") & (merged_df["판매월일치여부"] == "TRUE"))
    final_df["상품매출"] = final_df["상품ID"].map(merged_df[cond_sales].groupby("상품ID")["대변"].sum()).fillna(0)

    final_df["용역매출"] = 0
    consign_mask = final_df["매입유형1"].isin(["위탁", "위탁매입"])
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(위탁판매수수료)", "위탁", target_mask=consign_mask)

    final_df['매도/낙찰'] = 0
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(매도비)", "매도")
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(낙찰수수료)", "낙찰")
    final_df['매도/낙찰'] = final_df['매도'] + final_df['낙찰']

    finance_mask = final_df["상품/위탁"] == "상품"
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(금융수수료)", "금융수수료", target_mask=finance_mask, use_month_match=False)
    
    final_df['기타'] = 0 
    restore_mask = ((final_df["매입유형1"] == "선물") & (final_df["매입처"]=='현대캐피탈'))
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(원상회복비)", "원상회복", target_mask=restore_mask)
    annual_mask = final_df["소/도매"] == "도매"
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(연회비)", "연회비", target_mask=annual_mask)
    eval_mask = ((final_df["배정채널"] == "K") | (final_df["판매처"].str.contains("글로비스", na=False)))
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(평가사수수료)", "평가사수수료", target_mask=eval_mask)

    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(리본케어)", "리본케어")
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(리본케어플러스)", "리본케어플러스")
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(성능보증)", "성능보증")
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(탁송비)", "탁송비")

    final_df['기타'] = final_df['원상회복'] + final_df['연회비'] + final_df['평가사수수료'] + final_df['리본케어'] + final_df['리본케어플러스']  + final_df['성능보증'] + final_df['탁송비']
    final_df['용역매출'] = final_df['매도/낙찰'] + final_df['위탁'] + final_df['금융수수료'] + final_df['기타']
    final_df['매출합계'] = final_df['상품매출'] + final_df['용역매출']
  
    final_df['updated_at'] = datetime.now(ZoneInfo("Asia/Seoul")).strftime('%Y-%m-%d %H:%M:%S')

    return final_df

def save_to_master(new_df, verify_file=None, file_name="master_pnl.xlsx"):
    name_map = {
        '상품매출': '상품매출(자동차)', '원상회복': '수입수수료(원상회복비)', '연회비': '수입수수료(연회비)',
        '매도': '수입수수료(매도비)', '낙찰': '수입수수료(낙찰수수료)', '위탁': '수입수수료(위탁판매수수료)',
        '금융수수료': '수입수수료(금융수수료)', '성능보증': '수입수수료(성능보증)', '탁송비': '수입수수료(탁송비)',
        '리본케어' : '수입수수료(리본케어)', '리본케어플러스' : '수입수수료(리본케어플러스)', '평가사수수료' : '수입수수료(평가사수수료)'
    }
    
    for item in name_map.keys():
        new_df[f"{item}_검증"] = True

    verify_error = None
    if verify_file is not None:
        try:
            xl = pd.ExcelFile(verify_file)
            sheet_names = xl.sheet_names
            # '검증'이라는 글자가 포함된 시트를 찾고, 없으면 맨 앞의 첫 번째 시트를 사용합니다.
            target_sheet = next((s for s in sheet_names if '검증' in s), sheet_names[0])
            v_df = pd.read_excel(verify_file, sheet_name=target_sheet)
            v_month_cols = {}
            for col in v_df.columns:
                match = re.search(r'(\d{2,4})[-년\s]*(\d{1,2})[-월\s]*', str(col))
                if match:
                    v_month_cols[int(match.group(2))] = col

            # '계정명' 컬럼이 없는 경우에 대한 예외 처리
            if '계정명' not in v_df.columns:
                raise ValueError(f"시트('{target_sheet}')에서 '계정명' 컬럼을 찾을 수 없습니다.")

            for item, v_key in name_map.items():
                # regex=False를 추가하여 괄호()를 특수문자가 아닌 일반 문자로 취급하도록 수정
                v_row = v_df[v_df['계정명'].str.contains(v_key, na=False, case=False, regex=False)]
                if not v_row.empty:
                    for m, v_col in v_month_cols.items():
                        calc_val = new_df[new_df['판매월'] == m][item].sum()
                        actual_val = pd.to_numeric(v_row[v_col], errors='coerce').sum()
                        new_df.loc[new_df['판매월'] == m, f"{item}_검증"] = abs(calc_val - actual_val) < 100
        except Exception as e:
            verify_error = str(e)

    if os.path.exists(file_name):
        old_df = pd.read_excel(file_name)
        combined_df = pd.concat([old_df, new_df], ignore_index=True)
        combined_df = combined_df.drop_duplicates(subset=['상품ID'], keep='last')
    else:
        combined_df = new_df

    combined_df.to_excel(file_name, index=False)
    return file_name, verify_error