import streamlit as st
import pandas as pd
from utils.excel import to_excel_with_format
from processor import preprocess_sales_data
from analyzer import build_final_report

# 기본 설정
st.set_page_config(page_title="손익분석", layout="wide")
st.title("📊 손익분석")

# 탭 설정
tab1, tab2, tab3 = st.tabs(["매출", "UE", "summary"])

with tab1:
    # 1️⃣ 기준 데이터 업로드
    st.header("1️⃣ 판매차량")
    base_file = st.file_uploader("기준 엑셀 업로드", type=["xlsx"], key="base")
    
    if base_file:
        base_df = pd.read_excel(base_file)
        # 기본 처리
        base_df["판매일자"] = pd.to_datetime(base_df["판매일자"])
        base_df["판매연도"] = base_df["판매일자"].dt.year
        base_df["판매월"] = base_df["판매일자"].dt.month
        col = base_df.pop('판매월')
        base_df['판매월'] = col   # 판매월 맨 뒤로 보내는 작업

        #소/도매구분추가

        total_cnt = len(base_df)
        consign_cnt = (base_df['매입유형1'] == '위탁').sum()
        product_cnt = total_cnt - consign_cnt

        st.success("기준 데이터 업로드 완료")

        st.markdown(
            f"""
        **전체건:** {total_cnt:,}건 │
        **위탁:** {consign_cnt:,}건 │
        **상품:** {product_cnt:,}건 │
        **판매월:** {base_df['판매월'].min()}월
        """
        )

        st.dataframe(base_df)

        # 2️⃣ 자동 전처리 영역
        st.header("2️⃣ 판매 차량별 매출")
        uploaded_files = st.file_uploader("매출 엑셀 파일들 업로드", type=["xlsx"], accept_multiple_files=True)

        if uploaded_files:
            merged_df = preprocess_sales_data(uploaded_files, base_df)
            st.session_state['merged_df'] = merged_df # 세션에 저장
            st.success("파일 통합 및 전처리 완료")
            st.markdown(
                f"""
            **전체건:** {len(merged_df):,}건 │
            **대변합:** {merged_df['대변'].sum():,}원 │
            **판매월:** {merged_df['회계월'].min()}월
            """
            )
            st.dataframe(merged_df)

            st.download_button(
                label="⬇ 통합 결과 다운로드",
                data=to_excel_with_format(merged_df, highlight_after_col="관리항목2"),
                file_name="통합_매출_전처리.xlsx"
            )

        # 3️⃣ 최종 매출 파일 생성
        st.header("3️⃣ 최종 결과 산출")
        if st.button("▶ 최종 리포트 생성") and 'merged_df' in st.session_state:
            final_df = build_final_report(base_df, st.session_state['merged_df'])
            st.success("🎉 최종 매출 파일 생성 완료")
            st.dataframe(final_df)

            st.download_button(
                label="⬇ 최종 매출 다운로드",
                data=to_excel_with_format(final_df, highlight_after_col="판매월"),
                file_name="최종_매출_파일.xlsx"
            )


# UE / Summary 탭 (준비중)
with tab2: st.info("아직 준비중이다.")
with tab3: st.info("아직 준비중이다.")