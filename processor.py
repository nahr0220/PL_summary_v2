import pandas as pd
import numpy as np
import re

def preprocess_sales_data(uploaded_files, base_df):
    df_list = []
    for file in uploaded_files:
        df = pd.read_excel(file, engine="openpyxl", dtype=str)
        if "관리항목2" not in df.columns:
            continue
        # 관리항목2 까지 자르기
        end_idx = df.columns.get_loc("관리항목2") + 1
        df_list.append(df.iloc[:, :end_idx])

    if not df_list:
        return None

    merged_df = pd.concat(df_list, ignore_index=True, sort=False)
    
    # 기본 날짜 처리
    merged_df = merged_df[~merged_df['회계일자'].isin(['월계', '누계'])]
    merged_df['회계일자'] = pd.to_datetime(merged_df['회계일자'], format='mixed', errors='coerce')
    merged_df['회계연도'] = merged_df['회계일자'].dt.year
    merged_df['회계월'] = merged_df['회계일자'].dt.month
    merged_df['회계일자'] = merged_df['회계일자'].dt.date

    # 1️⃣ 분류 로직
    merged_df["분류"] = merged_df["계정명"].str.extract(r"\((.*?)\)")
    merged_df.loc[merged_df["계정명"] == "상품매출(자동차)", "분류"] = "상품매출"
    
    # 위탁판매수수료 세분화
    mask_consign = merged_df["계정명"] == "수입수수료(위탁판매수수료)"
    merged_df.loc[mask_consign & merged_df["적요"].str.contains("차액", na=False), "분류"] = "위탁판매수수료_차액"
    merged_df.loc[mask_consign & merged_df["적요"].str.contains("자산", na=False), "분류"] = "위탁판매수수료_자산"
    merged_df.loc[mask_consign & ~merged_df["적요"].str.contains("차액|자산", na=False), "분류"] = "위탁판매수수료"

    # 매도비 세분화
    mask_sell = merged_df["계정명"] == "수입수수료(매도비)"
    merged_df.loc[mask_sell & merged_df["적요"].str.contains("자산", na=False), "분류"] = "매도비_자산"
    merged_df.loc[mask_sell & ~merged_df["적요"].str.contains("자산", na=False), "분류"] = "매도비"

    # 낙찰수수료 세분화
    mask_auc = merged_df["계정명"] == "수입수수료(낙찰수수료)"
    cancel_words = "낙찰취소 수수료|낙찰취소 위약금|낙찰취소수수료|낙찰취소위약금"
    merged_df.loc[mask_auc & merged_df["적요"].str.contains(cancel_words, na=False), "분류"] = "낙찰수수료_취소수수료"
    merged_df.loc[mask_auc & merged_df["적요"].str.contains("자산|LC", na=False), "분류"] = "낙찰수수료_자산"
    merged_df.loc[mask_auc & merged_df["적요"].str.contains("외부|위탁", na=False), "분류"] = "낙찰수수료_외부출품"
    merged_df.loc[mask_auc & ~merged_df["적요"].str.contains(cancel_words + "|자산|LC|외부|위탁", na=False), "분류"] = "낙찰수수료"

    # 2️⃣ 차량번호 추출
    unit_pattern = r'(?:(?:서울|부산|대구|인천|광주|대전|울산|경기)?\d{2,3}[가-힣]\d{4}|지게차)'
    merged_df["차량번호"] = merged_df["적요"].str.extract(f"({unit_pattern})")

    # 3️⃣ 상품ID Lookup
    new_map = base_df.dropna(subset=["신차량번호"]).drop_duplicates("신차량번호").set_index("신차량번호")["상품ID"].to_dict()
    old_map = base_df.dropna(subset=["구차량번호"]).drop_duplicates("구차량번호").set_index("구차량번호")["상품ID"].to_dict()

    merged_df["상품ID"] = "확인필요"
    mask_blank = ((merged_df["계정명"] == "수입수수료(연회비)") | (merged_df["거래처"] == "결산거래처") | (merged_df["계정명"] == "수입수수료(상품화)"))
    merged_df.loc[mask_blank, "상품ID"] = ""
    
    mask_check = merged_df["분류"].isin(["낙찰수수료_취소수수료", "낙찰수수료_자산", "낙찰수수료_외부출품"])
    merged_df.loc[mask_check, "상품ID"] = "확인필요"

    # 지게차/일반 차량 매핑
    mask_forklift = merged_df["차량번호"] == "지게차"
    merged_df.loc[mask_forklift, "상품ID"] = merged_df.loc[mask_forklift, "적요"].str[:12]

    lookup_map = {**old_map, **new_map}
    mask_lookup = ~mask_blank & ~mask_check & ~mask_forklift
    merged_df.loc[mask_lookup, "상품ID"] = merged_df.loc[mask_lookup, "차량번호"].map(lookup_map)
    merged_df["상품ID"] = merged_df["상품ID"].fillna("확인필요")

    # 1. 취소 로직 (금액이 반대인 전표 찾기)
    merged_df['대변'] = pd.to_numeric(merged_df['대변'], errors='coerce').fillna(0)
    merged_df['abs_v'] = merged_df['대변'].abs()
    merged_df['seq'] = merged_df.groupby(
        ['회계연도', '회계월', '차량번호', 'abs_v', merged_df['대변'] > 0]
    ).cumcount()

    canceled = merged_df.groupby(
        ['회계연도', '회계월', '차량번호', 'abs_v', 'seq']
    )['대변'].transform('count') > 1
    
    merged_df['상태'] = np.where(canceled, '취소', '')
    
    # 2. 중복 체크 (동일 상품ID가 여러 번 등장하는지)
    merged_df['중복'] = (
        merged_df['상품ID'].replace(["", "확인필요"], np.nan).notna() & 
        merged_df['상품ID'].duplicated(keep=False)
    ).astype(int)

    # 정리용 임시 컬럼 삭제
    merged_df.drop(columns=['abs_v', 'seq'], inplace=True)


    # 4️⃣ 판매정보 연동 및 상태값 계산
    year_map = base_df.set_index("상품ID")["판매연도"]
    month_map = base_df.set_index("상품ID")["판매월"]
    merged_df["판매연도"] = merged_df["상품ID"].map(year_map)
    merged_df["판매월"] = merged_df["상품ID"].map(month_map)

    merged_df["판매월일치여부"] = ""

    mask = merged_df["판매월"].notna()

    merged_df.loc[mask, "판매월일치여부"] = (merged_df.loc[mask, "회계월"].eq(merged_df.loc[mask, "판매월"]).map({True: "TRUE", False: "FALSE"}))

    # 배부방식 결정
    merged_df["배부방식"] = "간접"
    cond1 = (merged_df["계정명"] == "수입수수료(금융수수료)") & (merged_df["상품ID"].str.startswith("C", na=False))
    cond2 = merged_df["판매월일치여부"] == "TRUE"
    merged_df.loc[cond1 | cond2, "배부방식"] = "직접"

    return merged_df