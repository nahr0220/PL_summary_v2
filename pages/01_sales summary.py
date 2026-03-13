import streamlit as st
import pandas as pd
from utils.excel import to_excel_with_format
from processor import preprocess_sales_data
from analyzer import build_final_report, save_to_master
from datetime import datetime
import os

# 기본 설정
st.set_page_config(page_title="손익분석", layout="wide")
st.title("1. sales summary")

# 탭 설정
tab1, tab2 = st.tabs(["VIEW", "UPLOAD"])


with tab1:        # VIEW
    st.header("🔍 sales summary")

    master_file = "master_pnl.xlsx"
    
    if os.path.exists(master_file) and os.path.getsize(master_file) > 0:
        # 1. 파일 정보 및 레이아웃 설정
        mtime = os.path.getmtime(master_file)
        last_updated = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
        
        col_space, col_btn = st.columns([8, 2]) # 버튼을 오른쪽으로 밀기 위한 컬럼
        
        with col_btn:
            # 삭제 버튼
            if st.button("🗑️ 전체 데이터 초기화", type="primary", use_container_width=True):
                st.session_state['delete_confirm'] = True
            
            # 🔥 버튼 바로 아래에 업데이트 시간 표시
            st.markdown(f"<p style='text-align: right; color: gray; font-size: 0.75rem; margin-top: -10px;'>* 최근 업데이트: {last_updated}</p>", unsafe_allow_html=True)
        
        # 3. 삭제 확인 절차 (실수 방지)
        if st.session_state.get('delete_confirm'):
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ 삭제", use_container_width=True):
                    os.remove(master_file)
                    st.session_state['delete_confirm'] = False
                    st.success("데이터가 완전히 삭제되었습니다.")
                    st.rerun()
            with c2:
                if st.button("❌ 취소", use_container_width=True):
                    st.session_state['delete_confirm'] = False
                    st.rerun()


        # 4. 데이터 로드 및 요약 지표
        master_df = pd.read_excel(master_file)
        
        # 판매월 멀티셀렉트 필터
        all_months = sorted(master_df['판매월'].unique())
        selected_months = st.multiselect("조회할 판매월 선택", all_months, default=all_months)
        
        # 필터링된 데이터
        display_df = master_df[master_df['판매월'].isin(selected_months)]
        
        # # 주요 지표 시각화 (Metric)
        # if not display_df.empty:
        #     m1, m2, m3, m4 = st.columns(4)
        #     m1.metric("총 매출액", f"{display_df['상품매출'].sum():,.0f}원")
        #     m2.metric("판매 대수", f"{len(display_df):,}대")
        #     m3.metric("평균 단가", f"{(display_df['상품매출'].sum() / len(display_df)):,.0f}원")
        #     m4.metric("위탁 비중", f"{(len(display_df[display_df['매입유형1']=='위탁']) / len(display_df) * 100):.1f}%")
        
        # 데이터프레임 출력
        st.dataframe(display_df, use_container_width=True)
        
        # 필터링된 데이터 다운로드
        st.download_button(
            label="⬇ 데이터 다운로드",
            data=to_excel_with_format(display_df, highlight_after_col="판매월"),
            file_name=f"누적데이터_조회_{datetime.now().strftime('%Y%m%d')}.xlsx"
        )
        
    else:
        st.info("📂 아직 저장된 데이터가 없습니다. UPLOAD 탭에서 데이터를 저장해 주세요.")


with tab2: # UPLOAD
    # 1️⃣ 기준 데이터 업로드
    st.header("1️⃣ sales data")
    base_file = st.file_uploader("기준 엑셀 업로드", type=["xlsx"], key="base")
    
    if base_file:
        base_df = pd.read_excel(base_file)
        # 전처리
        base_df["판매일자"] = pd.to_datetime(base_df["판매일자"])
        base_df["판매연도"] = base_df["판매일자"].dt.year
        base_df["판매월"] = base_df["판매일자"].dt.month
        # 판매월을 맨 뒤 컬럼으로 이동
        cols = [c for c in base_df.columns if c != '판매월'] + ['판매월']
        base_df = base_df[cols]

        # 요약 정보 출력
        total_cnt = len(base_df)
        consign_cnt = (base_df['매입유형1'] == '위탁').sum()
        product_cnt = total_cnt - consign_cnt
        st.markdown(f"**전체:** {total_cnt:,}건 │ **상품:** {product_cnt:,}건 │ **위탁:** {consign_cnt:,}건 │ **판매월:** {base_df['판매월'].min()}월 ~ {base_df['판매월'].max()}월")
        st.dataframe(base_df, use_container_width=True)

    # 2️⃣ 자동 전처리 영역
    st.divider()
    st.header("2️⃣ sales by account")
    uploaded_files = st.file_uploader("매출 엑셀 파일들 업로드", type=["xlsx"], accept_multiple_files=True)

    if uploaded_files and base_file: # base_file이 있을 때만 실행
        merged_df = preprocess_sales_data(uploaded_files, base_df)
        st.session_state['merged_df'] = merged_df 

        # 계정 필터 (12개 대응: multiselect 유지하되 가독성 확보)
        acc_col = '계정명' if '계정명' in merged_df.columns else '계정'
        all_accounts = sorted(merged_df[acc_col].unique())
        
        selected_accounts = st.multiselect(
            "계정 선택 (미선택 시 전체 조회)", 
            options=all_accounts,
            default=all_accounts
        )

        # 필터링 적용
        filtered_df = merged_df[merged_df[acc_col].isin(selected_accounts)] if selected_accounts else merged_df

        st.markdown(f"**필터 결과:** {len(filtered_df):,}건 │ **대변합:** {filtered_df['대변'].sum():,.0f}원 │ **회계월:** {filtered_df['회계월'].min()}월 ~ {filtered_df['회계월'].max()}월")
        st.dataframe(filtered_df, use_container_width=True)

        st.download_button(
            label="⬇ 엑셀 다운로드",
            data=to_excel_with_format(filtered_df, highlight_after_col="관리항목2"),
            file_name="통합_매출_전처리_필터.xlsx"
        )

        # 3️⃣ 최종 sales summary 산출
        st.divider()
        st.header("3️⃣ sales summary")
        
        if st.button("▶ 최종 생성"):
            final_df = build_final_report(base_df, st.session_state['merged_df'])
            st.session_state['current_final'] = final_df # 결과 세션 저장

        # 결과가 세션에 있을 때만 화면에 표시
        if 'current_final' in st.session_state:
            f_df = st.session_state['current_final']
            st.dataframe(f_df, use_container_width=True)

            st.download_button(
                label="⬇️ 엑셀 다운로드",
                data=to_excel_with_format(f_df, highlight_after_col="판매월"),
                file_name=f"final_summary.xlsx",
                use_container_width=True
            )

            # 마스터 저장 영역
            st.subheader("💾 마스터 파일 관리")
            if st.button("현재 결과를 마스터 파일에 누적 저장"):
                fname = save_to_master(f_df)
                st.success(f"✅ '{fname}' 누적 저장 완료")