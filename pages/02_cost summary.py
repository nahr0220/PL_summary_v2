import streamlit as st
import pandas as pd
import numpy as np
import re


st.set_page_config(page_title="손익분석", layout="wide")
st.title("Cost Summary")

tab1, tab2 = st.tabs(["VIEW", "UPLOAD"])

# with tab1:  # view (비용요약정보)







with tab2:  # upload (비용요약정보 업로드)
    st.header("1️⃣ 당사차량업로드")
    base_file = st.file_uploader("기준 파일 업로드", type=["xlsx"], key="base")
    if base_file:
        df_base = pd.read_excel(base_file)
        df_base = df_base[~df_base['회계일자'].isin(['월계', '누계', '전일이월'])]
        df_base['회계일자'] = pd.to_datetime(df_base['회계일자'], format='mixed', errors='coerce')
        df_base['회계연도'] = df_base['회계일자'].dt.year
        df_base['회계월'] = df_base['회계일자'].dt.month
        df_base['회계일자'] = df_base['회계일자'].dt.date

        def extract_reference(f):
            f = str(f) if pd.notna(f) else ''
            
            patterns = [
                r'C\d{11}_\d{2,3}[^\d]\d{4}',
                r'C\d{11}_[^\d]{3}',
                r'C\d{11}_[^\d]{2}\d{2,3}[^\d]\d{4}',
            ]
            
            for pattern in patterns:
                match = re.search(pattern, f)
                if match:
                    return match.group()
            
            return ''

        df_base['참고'] = df_base['적요'].apply(extract_reference)

        def classify_cost(row):
            f = str(row['적요']) if pd.notna(row['적요']) else ''
            ba = row['참고']
            
            def contains_any(keywords):
                return any(kw in f for kw in keywords)
            
            if contains_any(['매출원가', '재공품', '상품평가충당금']):
                return '결산'
            elif contains_any(['오류']):
                return '매입수수료'
            elif contains_any(['초과운행']):
                return '초과운행'
            elif contains_any(['계약만기 도래분(반납)']):
                return '페이백(반납)'
            elif contains_any(['계약만기 도래분(미반납)']):
                return '페이백(미반납)'
            elif contains_any(['폐자원']):
                return '폐자원공제'
            elif contains_any(['취득세', '취등록세']):
                return '취득세'
            elif contains_any(['선매입']):
                return '상품매입액'
            elif contains_any(['피알앤디컴퍼니', '경매장', '인품', '엔카', '중개', '알선', '매입',
                            '소개수수료', '헤이딜러', '매입수수료', '매입 수수료',
                            '낙찰수수료', '낙찰 수수료']):
                return '매입수수료'
            elif contains_any(['(상품->건설중인자산)', '상품->자산']):
                return '자산출고'
            elif contains_any(['상품전환']):
                return '타처입고'
            elif pd.notna(ba) and ba != 0:
                return '상품매입액'
            else:
                return ''

        df_base['원가구분'] = df_base.apply(classify_cost, axis=1)





        st.markdown(f"**전체:** {len(df_base):,}대")
        st.dataframe(df_base, width="stretch")

    st.divider()
    st.header("2️⃣ 비용요약정보 업로드")
    uploaded_files = st.file_uploader("비용요약정보 파일 업로드 (여러 개 가능)", type=["xlsx"], accept_multiple_files=True, key="cost_summary")

        
