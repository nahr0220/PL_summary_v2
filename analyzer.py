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
    인사정보는 구분자(판매월1일의 엑셀시리얼_관리용코드)가 디비 '구분자' 컬럼과 일치하는 행을 매칭."""
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

    # === 인사정보 (구분자 = 판매월1일(엑셀시리얼)_관리용코드 ↔ 디비 '구분자' 컬럼 매칭) ===
    need_hr = ["구분자", "관리용 코드", "팀명", "본부", "실", "관리명", "매입구분"]
    if hr_df is not None and not hr_df.empty and all(c in hr_df.columns for c in need_hr):
        hr_df = hr_df.copy()

        # 팀명 → 관리용 코드 (첫 매칭)
        team_to_code = (hr_df.dropna(subset=["팀명"])
                              .drop_duplicates("팀명")
                              .set_index("팀명")["관리용 코드"])
        # 디비에 이미 있는 '구분자' 컬럼 → 본부/실/관리명/매입구분
        hr_df["_gb"] = hr_df["구분자"].astype(str).str.strip()
        info = hr_df.dropna(subset=["구분자"]).drop_duplicates("_gb").set_index("_gb")

        # EOMONTH(판매월1일,-1)+1 = 판매 그 달 1일 → 엑셀 시리얼 (판매월 ↔ 결산월 일치)
        EXCEL_EPOCH = pd.Timestamp("1899-12-30")
        smonth = pd.to_datetime(
            pd.to_numeric(df["판매연도"], errors="coerce").astype("Int64").astype(str)
            + "-"
            + pd.to_numeric(df["판매월"], errors="coerce").astype("Int64").astype(str).str.zfill(2)
            + "-01",
            errors="coerce"
        )
        serial = (smonth - EXCEL_EPOCH).dt.days.astype("Int64").astype(str)

        def _code(point_col):
            c = df[point_col].map(team_to_code).astype(str).str.strip()
            return c.str.replace(r"\.0$", "", regex=True)  # 숫자코드의 .0 제거

        gb_buy  = serial + "_" + _code("매입지점")   # 매입팀 구분자(미노출)
        gb_sell = serial + "_" + _code("판매지점")   # 판매팀 구분자(미노출)

        df = df.drop(columns=["매입본부", "매입실", "매입팀", "매입구분", "판매본부", "판매실", "판매팀"], errors="ignore")

        # 매입 본부/실/팀(관리명) = 디비에서 구분자 일치 행의 값
        df["매입본부"] = gb_buy.map(info["본부"])
        df["매입실"]  = gb_buy.map(info["실"])
        df["매입팀"]  = gb_buy.map(info["관리명"])

        # 매입담당: 매입실 == '자산선환' 이면 매입팀, 아니면 매입사원
        df["매입담당"] = np.where(df["매입실"] == "자산선환", df["매입팀"], df["매입사원"])

        # 매입구분 = 디비에서 구분자 일치 행의 값 (매입담당 다음 순서)
        df["매입구분"] = gb_buy.map(info["매입구분"])

        # 판매 본부/실/팀(관리명) = 디비에서 구분자 일치 행의 값
        df["판매본부"] = gb_sell.map(info["본부"])
        df["판매실"]  = gb_sell.map(info["실"])
        df["판매팀"]  = gb_sell.map(info["관리명"])

        # 판매담당: 도매 → 판매팀 / 판매방식(기타·지점도매·도매/도매·도매/경매) → '도매/경매' / 그 외 → 판매사원
        cond_dome = df["소/도매"] == "도매"
        cond_way = df["판매방식"].isin(["기타/지점도매", "도매/도매", "도매/경매"])
        df["판매담당"] = np.select(
            [cond_dome, cond_way],
            [df["판매팀"], "도매/경매"],
            default=df["판매사원"]
        )

    # 구분자 컬럼을 '판매연도' 바로 왼쪽으로 이동 ('판매연도'가 없으면 현재 위치 유지)
    if ">>컬럼구분>>" in df.columns and "판매연도" in df.columns:
        cols = [c for c in df.columns if c != ">>컬럼구분>>"]
        idx = cols.index("판매연도")
        cols = cols[:idx] + [">>컬럼구분>>"] + cols[idx:]
        df = df[cols]

    # updated_at은 항상 맨 끝(오른쪽) 컬럼으로
    if "updated_at" in df.columns:
        cols = [c for c in df.columns if c != "updated_at"] + ["updated_at"]
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

VERIFY_NAME_MAP = {
    '상품매출': '상품매출(자동차)', '원상회복': '수입수수료(원상회복비)', '연회비': '수입수수료(연회비)',
    '매도': '수입수수료(매도비)', '낙찰': '수입수수료(낙찰수수료)', '위탁': '수입수수료(위탁판매수수료)',
    '금융수수료': '수입수수료(금융수수료)', '성능보증': '수입수수료(성능보증)', '탁송비': '수입수수료(탁송비)',
    '리본케어' : '수입수수료(리본케어)', '리본케어플러스' : '수입수수료(리본케어플러스)', '평가사수수료' : '수입수수료(평가사수수료)'
}

def compute_verification(df, verify_file=None):
    """df에 {item}_검증(일치여부)·{item}_검증값(검증파일 실제값) 컬럼 추가.
    저장 전 미리보기와 실제 저장(save_to_master)에서 동일하게 사용. (df, verify_error) 반환."""
    df = df.copy()
    for item in VERIFY_NAME_MAP.keys():
        df[f"{item}_검증"] = True
        df[f"{item}_검증값"] = np.nan   # 검증파일 실제값 (없으면 NaN)

    verify_error = None
    if verify_file is not None:
        if hasattr(verify_file, "seek"):
            try:
                verify_file.seek(0)   # 미리보기·저장에서 중복 읽힐 수 있어 위치 초기화
            except Exception:
                pass
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

            for item, v_key in VERIFY_NAME_MAP.items():
                # regex=False를 추가하여 괄호()를 특수문자가 아닌 일반 문자로 취급하도록 수정
                v_row = v_df[v_df['계정명'].str.contains(v_key, na=False, case=False, regex=False)]
                if not v_row.empty:
                    for m, v_col in v_month_cols.items():
                        calc_val = df[df['판매월'] == m][item].sum()
                        actual_val = pd.to_numeric(v_row[v_col], errors='coerce').sum()
                        df.loc[df['판매월'] == m, f"{item}_검증"] = round(calc_val) == round(actual_val)
                        df.loc[df['판매월'] == m, f"{item}_검증값"] = actual_val
        except Exception as e:
            verify_error = str(e)
    return df, verify_error

def save_to_master(new_df, verify_file=None, file_name="master_pnl.xlsx"):
    new_df, verify_error = compute_verification(new_df, verify_file)
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

    # '>>컬럼구분>>'를 '판매연도' 바로 왼쪽으로 강제 정렬 (기존 master 순서에 영향받지 않도록)
    if ">>컬럼구분>>" in combined_df.columns and "판매연도" in combined_df.columns:
        cols = [c for c in combined_df.columns if c != ">>컬럼구분>>"]
        idx = cols.index("판매연도")
        cols = cols[:idx] + [">>컬럼구분>>"] + cols[idx:]
        combined_df = combined_df[cols]

    # 'updated_at'은 항상 맨 마지막 컬럼으로
    if "updated_at" in combined_df.columns:
        cols = [c for c in combined_df.columns if c != "updated_at"] + ["updated_at"]
        combined_df = combined_df[cols]

    combined_df.to_excel(file_name, index=False)
    return file_name, verify_error