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

def enrich_vendor_hr(df, vendor_df=None, hr_df=None):
    """저장 직전 f_df에 거래처(매입처 구분)·인사정보(매입/판매 본부·실·팀·담당)를 결합한다.
    인사정보는 판매연도/판매월(해당 월 1일) 기준으로 적용시점 <= 판매기준일 중 최신 행을 매칭."""
    df = df.copy()

    # === 거래처(매입처 구분) ===
    if vendor_df is not None and not vendor_df.empty:
        v_map = vendor_df.drop_duplicates("거래처")
        df = df.drop(columns=["거래처_정정", "매입처 구분", "거래처"], errors="ignore")
        df = df.merge(v_map[["거래처", "거래처_정정"]], left_on="매입처", right_on="거래처", how="left")
        df = df.drop(columns=["거래처"], errors="ignore")
        df = df.rename(columns={"거래처_정정": "매입처 구분"})
        df["매입처 구분"] = df["매입처 구분"].fillna("기타")

    # 구분자 컬럼 (위치는 함수 끝에서 '계약서' 뒤로 재배치)
    df = df.drop(columns=[">>컬럼구분>>"], errors="ignore")
    df[">>컬럼구분>>"] = ""

    # === 인사정보 (적용시점 기준 merge_asof) ===
    if hr_df is not None and not hr_df.empty:
        hr_df = hr_df.copy()
        hr_df["적용시점"] = pd.to_datetime(hr_df["적용시점"])
        hr_sorted = hr_df[["적용시점", "팀", "본부", "실", "팀_정정"]].sort_values("적용시점")

        df = df.drop(columns=["매입본부", "매입실", "매입팀", "판매본부2", "판매실", "판매팀"], errors="ignore")
        df["_판매기준일"] = pd.to_datetime(
            df["판매연도"].astype(str) + "-" + df["판매월"].astype(str).str.zfill(2) + "-01"
        )

        # 매입지점 매칭
        df = df.sort_values("_판매기준일").reset_index(drop=True)
        df = pd.merge_asof(
            df, hr_sorted, left_on="_판매기준일", right_on="적용시점",
            left_by="매입지점", right_by="팀", direction="backward"
        )
        df = df.rename(columns={"본부": "매입본부", "실": "매입실", "팀_정정": "매입팀"})
        df = df.drop(columns=["팀", "적용시점"], errors="ignore")

        df.loc[df["매입유형1"] == "자산", ["매입팀"]] = "자산"
        df["매입담당"] = df["매입사원"]

        cond1 = df["매입실"] == "상품매입실"
        cond2 = df["매입실"] == "옥션사업실"
        cond3 = df["매입팀"].str.endswith("지점", na=False)
        cond4 = df["매입팀"].str.endswith("파트", na=False)
        df["매입구분"] = np.select(
            [cond1, cond2, cond3, cond4],
            ["상품매입실", "기타", "지점", "지점"],
            default="기타"
        )

        # 판매지점 매칭
        df = df.sort_values("_판매기준일").reset_index(drop=True)
        df = pd.merge_asof(
            df, hr_sorted, left_on="_판매기준일", right_on="적용시점",
            left_by="판매지점", right_by="팀", direction="backward"
        )
        df = df.rename(columns={"본부": "판매본부2", "실": "판매실", "팀_정정": "판매팀"})
        df = df.drop(columns=["팀", "적용시점", "_판매기준일"], errors="ignore")

        cond1 = df["소/도매"] == "도매"
        cond2 = df["판매방식"].isin(["기타/지점도매", "도매/도매", "도매/경매"])
        df["판매담당"] = np.select(
            [cond1, cond2],
            [df["판매팀"], "#지점도매"],
            default=df["판매사원"]
        )

    # 구분자 컬럼을 '계약서' 바로 뒤로 이동 ('계약서'가 없으면 현재 위치 유지)
    if ">>컬럼구분>>" in df.columns and "계약서" in df.columns:
        cols = [c for c in df.columns if c != ">>컬럼구분>>"]
        idx = cols.index("계약서") + 1
        cols = cols[:idx] + [">>컬럼구분>>"] + cols[idx:]
        df = df[cols]

    return df

def build_final_report(base_df, merged_df):
    # col = ['매입가', '매입원가', '매입부가세', '폐자원공제액', '상품화비용', '판매가', '판매원가', '판매부가세', '매출액', 
    #        '매출이익', '매도비', '낙찰수수료', '매도비/낙찰수수료', '연장보증료', '찾아서', '추가상품', '성능보험료', '매출이익(목표기준)', 
    #        '위탁매입수수료', '위탁판매수수료', '위탁수수료', '매출총이익', '가치보장서비스', '판매목표가', '판매목표가차액', '지점판매가', 
    #        '지점판매가차액', '알선수수료', '임직원할인율', '임직원할인금액', '현금', '카드', '할부', '리스', '금융구분', '차량ID', '도/소매구분', '고객타입', '사업자유형', '업태', '업종']
    col = ['재고일','매입가','매입공급가액','매입부가세','폐자원공제액','매입채널수수료','매입취득세[E]','매입부대비용[E]','상품매입원가[E]','표준상품제조원가','상품화실비',
           '판매본부','판매가','판매공급가액','판매부가세','상품매출액[E]','매출이익','상품매출원가[E]','상품매출총이익[E]','매도비','낙찰수수료','매도비/낙찰수수료',
           '연장보증료','찾아서','추가상품','성능보험료','위탁수수료[E]','부가매출[E]','매출총이익[E]','가치보장서비스','판매목표가','판매목표가차액','지점판매가','지점판매가차액','알선수수료','임직원할인율','임직원할인금액',
            '현금','카드','할부','리스','금융구분','매입처 구분']
    
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

    finance_mask = (final_df["상품/위탁"] == "상품") & (final_df["소/도매"] == "소매")
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
        new_df[f"{item}_검증값"] = np.nan   # 검증파일 실제값 (없으면 NaN)

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
                        new_df.loc[new_df['판매월'] == m, f"{item}_검증값"] = actual_val
        except Exception as e:
            verify_error = str(e)
    # 기존코드
    # if os.path.exists(file_name):
    #     old_df = pd.read_excel(file_name)
    #     combined_df = pd.concat([old_df, new_df], ignore_index=True)
    #     combined_df = combined_df.drop_duplicates(subset=['상품ID'], keep='last')
    # else:
    #     combined_df = new_df
    # 해당월만 새로 업데이트로 수정
    if os.path.exists(file_name):
        old_df = pd.read_excel(file_name)

        # 판매월 컬럼 숫자형 정리
        old_df["판매월"] = pd.to_numeric(old_df["판매월"], errors="coerce")
        new_df["판매월"] = pd.to_numeric(new_df["판매월"], errors="coerce")

        # 새 데이터에 포함된 판매월 목록 추출
        update_months = new_df["판매월"].dropna().unique()

        # 기존 데이터에서 새 데이터에 포함된 판매월 삭제
        old_df = old_df[~old_df["판매월"].isin(update_months)]

        # 기존 데이터 + 새 데이터 결합
        combined_df = pd.concat([old_df, new_df], ignore_index=True)

    else:
        combined_df = new_df

    # '>>컬럼구분>>'를 '계약서' 바로 뒤로 강제 정렬 (기존 master 순서에 영향받지 않도록)
    if ">>컬럼구분>>" in combined_df.columns and "계약서" in combined_df.columns:
        cols = [c for c in combined_df.columns if c != ">>컬럼구분>>"]
        idx = cols.index("계약서") + 1
        cols = cols[:idx] + [">>컬럼구분>>"] + cols[idx:]
        combined_df = combined_df[cols]

    # 'updated_at'은 항상 맨 마지막 컬럼으로
    if "updated_at" in combined_df.columns:
        cols = [c for c in combined_df.columns if c != "updated_at"] + ["updated_at"]
        combined_df = combined_df[cols]

    combined_df.to_excel(file_name, index=False)
    return file_name, verify_error