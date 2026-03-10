import pandas as pd
import os

def build_final_report(base_df, merged_df):
    final_df = base_df.copy()

    # 특정 분류의 합계를 가져오는 헬퍼 함수
    def get_direct_sum(category_name):
        cond = (merged_df["분류"] == category_name) & (merged_df["판매월일치여부"] == "TRUE")
        return merged_df[cond].groupby("상품ID")["대변"].sum()

    # 1. 상품매출
    final_df["상품매출"] = final_df["상품ID"].map(get_direct_sum("상품매출")).fillna(0)

    final_df['용역매출'] = 0
    final_df['위탁판매수수료'] = 0
    final_df['매도/낙찰'] = 0
    final_df['매도비'] = 0
    
    # 2. 매도비_직
    final_df["매도비_직"] = final_df["상품ID"].map(get_direct_sum("매도비")).fillna(0)

    #매도비_간

    #final_df["매도비"] = final_df["매도비_직"] + final_df["매도비_간"]
    
    final_df['낙찰수수료'] = 0
    
    # 3. 낙찰수수료
    final_df["낙찰_직"] = final_df["상품ID"].map(get_direct_sum("낙찰수수료")).fillna(0)

    #낙찰_간

    #final_df["낙찰수수료"] = final_df["낙찰_직"] + final_df["낙찰_간"]
    #final_df["매도/낙찰"] = final_df["매도비"] + final_df["낙찰수수료"]

    #4. 금융수수료

    #5. 기타(원상회복비+연회비)
    final_df['기타'] = 0
    final_df['원상회복'] = 0

    # 원상회복_직
    final_df["원복_직"] = final_df["상품ID"].map(get_direct_sum("원상회복비")).fillna(0)
    # 원상회복_간
    # 연회비_간
    # 평가사수수료
    # 평가사수수료_직
    # 평가사수수료_간

    # 리본케어
    final_df['리본케어'] = 0
    final_df["리본케어_직"] = final_df["상품ID"].map(get_direct_sum("리본케어")).fillna(0)
    # 리본케어_간

    # final_df['리본케어'] = final_df["리본케어_직"] + final_df["리본케어_간"]

    # 리본케어플러스
    final_df['리본케어플러스'] = 0
    final_df["리본케어플러스_직"] = final_df["상품ID"].map(get_direct_sum("리본케어플러스")).fillna(0)
    # 리본케어플러스_간
    # final_df['리본케어플러스'] = final_df["리본케어플러스_직"] + final_df["리본케어플러스_간"]

    # 성능보증

    # 탁송비
    final_df['탁송비'] = 0
    final_df["탁송비_직"] = final_df["상품ID"].map(get_direct_sum("탁송비")).fillna(0)
    #탁송비_간
    # final_df['탁송비'] = final_df["탁송비_직"] + final_df["탁송비_간"]



    return final_df

# 🔥 마스터 파일 누적 함수
def save_to_master(new_df, file_name="master_pnl.xlsx"):
    if os.path.exists(file_name):
        # 1. 기존 파일이 있으면 읽어옴
        old_df = pd.read_excel(file_name)
        # 2. 새 데이터와 합침
        combined_df = pd.concat([old_df, new_df], ignore_index=True)
        # 3. 중복 제거 (상품ID가 같으면 최신 데이터만 남김)
        combined_df = combined_df.drop_duplicates(subset=['상품ID'], keep='last')
    else:
        # 파일이 없으면 그냥 현재 데이터가 마스터가 됨
        combined_df = new_df
    
    # 4. 엑셀로 저장
    combined_df.to_excel(file_name, index=False)
    return file_name