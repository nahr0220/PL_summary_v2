import streamlit as st
import pandas as pd

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

        
