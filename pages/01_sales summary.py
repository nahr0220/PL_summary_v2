import streamlit as st
import pandas as pd
from utils.excel import to_excel_with_format
from processor import preprocess_sales_data
from analyzer import build_final_report, save_to_master
from datetime import datetime
import os

# 기본 설정
st.set_page_config(page_title="손익분석", layout="wide")
st.title("Sales Summary")

# 탭 설정
tab1, tab2 = st.tabs(["VIEW", "UPLOAD"])


with tab1:        # VIEW
    master_file = "master_pnl.xlsx"

    if os.path.exists(master_file) and os.path.getsize(master_file) > 0:
        # 1. 파일 정보 및 레이아웃 설정
        mtime = os.path.getmtime(master_file)
        last_updated = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
        
        col_space, col_btn = st.columns([8, 2]) # 버튼을 오른쪽으로 밀기 위한 컬럼
        
        with col_btn:
            # 삭제 버튼
            if st.button("🗑️ 전체 데이터 초기화", type="primary", width="stretch"):
                st.session_state['delete_confirm'] = True
            
            # 🔥 버튼 바로 아래에 업데이트 시간 표시
            st.markdown(f"<p style='text-align: right; color: gray; font-size: 0.75rem; margin-top: -10px;'>* 최근 업데이트: {last_updated}</p>", unsafe_allow_html=True)
        
        # 3. 삭제 확인 절차 (실수 방지)
        if st.session_state.get('delete_confirm'):
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ 삭제", width="stretch"):
                    os.remove(master_file)
                    st.session_state['delete_confirm'] = False
                    st.success("데이터가 완전히 삭제되었습니다.")
                    st.rerun()
            with c2:
                if st.button("❌ 취소", width="stretch"):
                    st.session_state['delete_confirm'] = False
                    st.rerun()


        # 4. 데이터 로드 및 요약 지표
        master_df = pd.read_excel(master_file)
        order = ['소매', '도매']
        master_df['소/도매'] = pd.Categorical(master_df['소/도매'], categories=order, ordered=True)

        with st.expander("더존 PL"):
            indirect_items = ['원상회복', '연회비', '매도', '낙찰', '위탁', '평가사수수료', '금융수수료', '리본케어','리본케어플러스', '성능보증', '탁송비']
            
            # 컬럼 이름을 '1월' 대신 숫자 1, 2... 형태로 관리
            all_months_numeric = list(range(1, 13)) 
            monthly_data = []

            # 1. [전체 합계]
            if '매출합계' in master_df.columns:
                total_sum = master_df.groupby('판매월')['매출합계'].sum()
                for m in all_months_numeric:
                    monthly_data.append({
                        "항목": "00. 총합계", "구분": " ", 
                        "판매월": m, "금액": total_sum.get(m, 0)
                    })

            # 2. [상품매출]
            if '상품매출' in master_df.columns:
                s_df = master_df.groupby('판매월')['상품매출'].sum()
                for m in all_months_numeric:
                    monthly_data.append({
                        "항목": "01. 상품매출", "구분": " ", 
                        "판매월": m, "금액": s_df.get(m, 0)
                    })

            # 3. [용역/수수료 항목들]
            for i, item in enumerate(indirect_items, start=2):
                if item in master_df.columns:
                    display_name = f"{i:02d}. {item}"
                    
                    # 월별로 그룹화해서 합계 계산
                    agg_df = master_df.groupby('판매월').agg({
                        item: "sum",
                        f"{item}_직": "sum" if f"{item}_직" in master_df.columns else lambda x: 0,
                        f"{item}_간": "sum" if f"{item}_간" in master_df.columns else lambda x: 0
                    })
                    
                    for m in all_months_numeric:
                        val_total = agg_df.loc[m, item] if m in agg_df.index else 0
                        val_dir = agg_df.loc[m, f"{item}_직"] if m in agg_df.index else 0
                        val_ind = agg_df.loc[m, f"{item}_간"] if m in agg_df.index else 0
                        
                        monthly_data.append({"항목": display_name, "구분": " ", "판매월": m, "금액": val_total})
                        monthly_data.append({"항목": display_name, "구분": "1. 직접", "판매월": m, "금액": val_dir})
                        monthly_data.append({"항목": display_name, "구분": "2. 간접", "판매월": m, "금액": val_ind})

            if monthly_data:
                final_df = pd.DataFrame(monthly_data)
                
                # 피벗 생성 (1~12 숫자가 컬럼이 됨)
                pivot_df = final_df.pivot_table(
                    index=["항목", "구분"], 
                    columns="판매월", 
                    values="금액",
                    aggfunc="sum",
                    fill_value=0,
                    observed=False
                )
                
                # 1~12월 순서 보장 및 'Mixed Type' 경고 방지를 위해 컬럼명을 문자로 변환
                pivot_df = pivot_df.reindex(columns=all_months_numeric, fill_value=0)
                pivot_df.columns = pivot_df.columns.astype(str) # '1', '2' ... 형태로 변환
                
                def make_bold(s):
                    is_total = s.name[1] == " "
                    return ['background-color: #f8f9fb; font-weight: bold' if is_total else '' for _ in s]

                def format_zero_to_dash(v):
                    return "-" if v == 0 else f"{v:,.0f}"

                # 최종 출력
                st.dataframe(
                    pivot_df.style
                    .apply(make_bold, axis=1)
                    .format(format_zero_to_dash),
                    width="stretch"
                )
            else:
                st.warning("데이터가 없습니다.")
        

        def style_dataframe(df):
            # 0은 '-'로, 나머지는 천 단위 콤마로 표시하는 포맷 함수
            # 문자열로 변환되므로 우측 정렬 속성이 중요합니다.
            format_func = lambda x: '-' if x == 0 else f"{x:,.0f}"
            
            return df.style.format(format_func) \
                .set_properties(**{
                    'text-align': 'right', 
                    'font-family': 'Malgun Gothic',
                    'font-size': '13px'
                }) \
                .apply(lambda x: [
                    'background-color: #e6f3ff; font-weight: bold; border-top: 2px solid #004c99' 
                    if (x.name[0] == '전체' or x.name == '합계(전체)') 
                    else '' for _ in x
                ], axis=1) \
                .set_table_styles([
                    {'selector': 'th', 'props': [('background-color', '#f8f9fa'), ('text-align', 'center')]}
                ])

        # 월별 판매 대수 출력
        if not master_df.empty:
            st.markdown("##### 월별 판매 대수")
            
            summary_pivot = master_df.pivot_table(
                index=['상품/위탁', '소/도매'],
                columns='판매월',
                values='상품ID',
                aggfunc='count',
                fill_value=0,
                observed=False
            ).astype(int) # 먼저 정수형으로 확정

            summary_pivot = summary_pivot.reindex(columns=range(1, 13), fill_value=0)
            summary_pivot['연간 총합'] = summary_pivot.sum(axis=1)
            
            # 합계 행 추가
            total_row = summary_pivot.sum(axis=0)
            summary_pivot.loc[('전체', '월별 총합'), :] = total_row

            # 스타일 적용 후 출력
            st.write(style_dataframe(summary_pivot))

        # 월별 매출 출력
        if not master_df.empty:
            st.markdown("##### 월별 매출")
            
            melted_revenue = master_df.melt(
                id_vars=['소/도매', '판매월'],
                value_vars=['상품매출', '용역매출'],
                var_name='매출항목',
                value_name='금액'
            )
            
            rev_pivot = melted_revenue.pivot_table(
                index=['매출항목', '소/도매'], 
                columns='판매월',
                values='금액',
                aggfunc='sum',
                fill_value=0,
                observed=False
            ).astype(int)

            rev_pivot = rev_pivot.reindex(columns=range(1, 13), fill_value=0)
            rev_pivot['연간 총합'] = rev_pivot.sum(axis=1)

            # 합계 행 추가
            rev_total_row = rev_pivot.sum(axis=0)
            rev_pivot.loc[('전체', '합계'), :] = rev_total_row

            # 스타일 적용 후 출력
            st.write(style_dataframe(rev_pivot))
            st.divider()
        
        # 멀티셀렉트 필터
        col1, col2 = st.columns(2)

        with col1:
            all_years = sorted(master_df['판매연도'].dropna().unique())
            selected_years = st.multiselect("판매연도", all_years, default=all_years)

        with col2:
            all_months = sorted(master_df['판매월'].dropna().unique())
            selected_months = st.multiselect("판매월", all_months, default=all_months)

        # 데이터 필터링
        display_df = master_df[(master_df['판매연도'].isin(selected_years)) & (master_df['판매월'].isin(selected_months))]

        # 주요 지표
        counts = display_df['매입유형1'].value_counts()
        st.markdown(f"**건수:** {len(display_df):,}건 │ **상품:** {len(display_df) - counts.get('위탁', 0):,}건 │ **위탁:** {counts.get('위탁', 0):,}건 │ **매출합계:** {display_df['매출합계'].sum():,.0f}원 │ **판매월:** {display_df['판매월'].min()}월 ~ {display_df['판매월'].max()}월")

        st.dataframe(display_df, width="stretch")
        
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
        st.dataframe(base_df, width="stretch")

    # 2️⃣ 자동 전처리 영역
    st.divider()
    st.header("2️⃣ sales by account")
    uploaded_files = st.file_uploader("매출 엑셀 파일들 업로드", type=["xlsx"], accept_multiple_files=True)

    if uploaded_files and base_file: # base_file이 있을 때만 실행
        merged_df = preprocess_sales_data(uploaded_files, base_df)
        st.session_state['merged_df'] = merged_df 

        year_col = '판매연도'
        month_col = '판매월'

        all_years = sorted(merged_df["판매연도"].dropna().unique())
        all_months = sorted(merged_df["판매월"].dropna().unique())

        col1, col2 = st.columns(2)

        with col1:
            selected_years = st.multiselect(
                "판매연도",
                options=all_years,
                default=all_years
            )

        with col2:
            selected_months = st.multiselect(
                "판매월",
                options=all_months,
                default=all_months
            )

        # 계정 필터
        acc_col = '계정명' if '계정명' in merged_df.columns else '계정'
        all_accounts = sorted(merged_df[acc_col].dropna().unique())

        selected_accounts = st.multiselect("계정 선택", options=all_accounts, default=all_accounts)

        # 필터 적용
        filtered_df = merged_df[(merged_df[year_col].isin(selected_years)) & (merged_df[month_col].isin(selected_months)) & (merged_df[acc_col].isin(selected_accounts))]
        st.markdown(f"**필터 결과:** {len(filtered_df):,}건 │ **대변합:** {filtered_df['대변'].sum():,.0f}원 │ **회계월:** {filtered_df['회계월'].min()}월 ~ {filtered_df['회계월'].max()}월")
        st.dataframe(filtered_df, width="stretch")

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
            st.dataframe(f_df, width="stretch")

            st.download_button(
                label="⬇️ 엑셀 다운로드",
                data=to_excel_with_format(f_df, highlight_after_col="판매월"),
                file_name=f"final_summary.xlsx",
                width="stretch"
            )

            # 마스터 저장 영역
            st.subheader("💾 마스터 파일 관리")
            if st.button("현재 결과를 마스터 파일에 누적 저장"):
                fname = save_to_master(f_df)
                st.success(f"✅ '{fname}' 누적 저장 완료")