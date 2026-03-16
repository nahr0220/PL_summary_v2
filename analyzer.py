import pandas as pd
import numpy as np
import os

def distribute_indirect_cost(df, merged_df, category_name, col_name, target_mask=None):
    df[col_name] = 0

    # 직접비 매칭
    cond = ((merged_df["계정명"] == category_name) & (merged_df["판매월일치여부"] == "TRUE"))

    direct_map = merged_df[cond].groupby("상품ID")["대변"].sum()
    df[f"{col_name}_직"] = df["상품ID"].map(direct_map).fillna(0)

    # 전체 비용
    total_fee = merged_df.loc[merged_df["계정명"] == category_name, "대변"].sum()

    # 간접비 총액
    indirect_total = total_fee - df[f"{col_name}_직"].sum()

    # mask 결정
    if target_mask is None:
        mask = df[f"{col_name}_직"] > 0
    else:
        mask = target_mask

    n = mask.sum()

    df[f"{col_name}_간"] = 0

    if n > 0 and indirect_total != 0:
        base_val = round(indirect_total / n)
        df.loc[mask, f"{col_name}_간"] = base_val
        diff = indirect_total - df.loc[mask, f"{col_name}_간"].sum()
        df.loc[df.index[mask][0], f"{col_name}_간"] += diff

    df[col_name] = df[f"{col_name}_직"] + df[f"{col_name}_간"]

    return df


def build_final_report(base_df, merged_df):

    final_df = base_df.copy()

    # 상품/위탁 
    final_df["상품/위탁"] = np.where(final_df["매입유형1"] == "위탁", "위탁", "상품")
    final_df["소/도매"] = np.where(final_df["판매지점"].str.contains("리본카옥션", na=False),"도매", "소매")

    final_df["매출합계"] = 0

    # 상품매출
    cond_sales = ((merged_df["계정명"] == "상품매출(자동차)") & (merged_df["판매월일치여부"] == "TRUE"))
    final_df["상품매출"] = final_df["상품ID"].map(merged_df[cond_sales].groupby("상품ID")["대변"].sum()).fillna(0)

    final_df["용역매출"] = 0

    # 위탁판매수수료
    consign_mask = final_df["매입유형1"].isin(["위탁", "위탁매입"])
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(위탁판매수수료)", "위탁", target_mask=consign_mask)

    final_df['매도/낙찰'] = 0

    # 기본 배부
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(매도비)", "매도")
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(낙찰수수료)", "낙찰")
    final_df['매도/낙찰'] = final_df['매도'] + final_df['낙찰']

    # 금융수수료
    finance_mask = final_df["상품/위탁"] == "상품"
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(금융수수료)", "금융수수료", target_mask=finance_mask)
    
    final_df['기타'] = 0

    # 원상회복비
    restore_mask = ((final_df["매입유형1"] == "선물") & (final_df["매입처"]=='현대캐피탈'))
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(원상회복비)", "원상회복", target_mask=restore_mask)

    # 연회비
    annual_mask = final_df["소/도매"] == "도매"
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(연회비)", "연회비", target_mask=annual_mask)

    # 평가사수수료
    eval_mask = ((final_df["배정채널"] == "K") | (final_df["판매처"].str.contains("글로비스", na=False)))
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(평가사수수료)", "평가사수수료", target_mask=eval_mask)

    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(리본케어)", "리본케어")
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(리본케어플러스)", "리본케어플러스")
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(성능보증)", "성능보증")
    final_df = distribute_indirect_cost(final_df, merged_df, "수입수수료(탁송비)", "탁송비")

    final_df['기타'] = final_df['원상회복'] + final_df['연회비'] + final_df['평가사수수료'] + final_df['리본케어'] + final_df['리본케어플러스']  + final_df['성능보증'] + final_df['탁송비']
    final_df['용역매출'] = final_df['매도/낙찰'] + final_df['위탁'] + final_df['금융수수료'] + final_df['기타']
    final_df['매출합계'] = final_df['상품매출'] + final_df['용역매출']

    return final_df

# 마스터 파일 누적 함수
def save_to_master(new_df, file_name="master_pnl.xlsx"):
    if os.path.exists(file_name):
        old_df = pd.read_excel(file_name)  #기존
        combined_df = pd.concat([old_df, new_df], ignore_index=True)  # 신규
        combined_df = combined_df.drop_duplicates(subset=['상품ID'], keep='last')  # 중복 제거
    else:
        combined_df = new_df          # 파일이 없으면 그냥 현재 데이터가 마스터가 됨

    combined_df.to_excel(file_name, index=False)      # 엑셀 저장
    return file_name