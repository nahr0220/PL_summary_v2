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


with tab1:
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


with tab2:
    # 1️⃣ 기준 데이터 업로드
    st.header("1️⃣ sales data")
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
        **상품:** {product_cnt:,}건 │
        **위탁:** {consign_cnt:,}건 │
        **판매월:** {base_df['판매월'].min()}월
        """
        )

        st.dataframe(base_df)



# 2️⃣ 자동 전처리 영역
        st.header("2️⃣ sales by account")
        uploaded_files = st.file_uploader("매출 엑셀 파일들 업로드", type=["xlsx"], accept_multiple_files=True)

        if uploaded_files:
            merged_df = preprocess_sales_data(uploaded_files, base_df)
            st.session_state['merged_df'] = merged_df # 세션에 저장
            
            st.success("파일 통합 및 전처리 완료")
            st.markdown(f"**전체건:** {len(merged_df):,}건 │ **대변합:** {merged_df['대변'].sum():,}원│ **판매월:** {merged_df['판매월'].min()}월")
            st.dataframe(merged_df)

            st.download_button(
                label="⬇ 통합 결과 다운로드",
                data=to_excel_with_format(merged_df, highlight_after_col="관리항목2"),
                file_name="통합_매출_전처리.xlsx"
            )

            # --- 3번 영역 시작 ---
            st.divider()
            st.header("3️⃣ sales summary 산출")
            
            if st.button("▶ 최종 리포트 생성"):
                # 1. 최종 리포트 생성 로직 실행
                final_df = build_final_report(base_df, st.session_state['merged_df'])
                st.session_state['current_final'] = final_df # 세션에 저장
                
                # 2. 요약 수치 계산 (3번 아래에 보여줄 정보)
                f_total_cnt = len(final_df)
                f_consign_cnt = (final_df['매입유형1'] == '위탁').sum()
                f_product_cnt = f_total_cnt - f_consign_cnt
                f_min_month = final_df['판매월'].min()
                f_max_month = final_df['판매월'].max()
                
                # 3. 요약 정보 출력
                st.markdown(
                    f"""
                    ### 📋 최종 리포트 요약
                    **전체건:** {f_total_cnt:,}건 │ 
                    **위탁:** {f_consign_cnt:,}건 │ 
                    **상품:** {f_product_cnt:,}건 │ 
                    **판매월:** {f_min_month}월 ~ {f_max_month}월
                    """
                )
                
                st.success("🎉 최종 리포트가 생성되었습니다!")
                st.dataframe(final_df)

            # ✨ 마스터 저장 버튼 (결과가 있을 때만 노출)
            if 'current_final' in st.session_state:
                st.markdown("---")
                st.subheader("💾 마스터 파일 관리")
                if st.button("현재 결과를 마스터 파일에 누적 저장"):
                    fname = save_to_master(st.session_state['current_final'])
                    st.success(f"✅ '{fname}'에 누적 저장이 완료되었습니다!")
                # 저장 후에는 중복 저장을 막기 위해 세션을 비울 수도 있습니다 (선택)
                # del st.session_state['current_final']