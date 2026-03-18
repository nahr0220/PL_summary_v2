import streamlit as st
import pandas as pd
from utils.excel import to_excel_with_format
from processor import preprocess_sales_data
from analyzer import build_final_report, save_to_master
from datetime import datetime
import os

st.set_page_config(page_title="손익분석", layout="wide")
st.title("Sales Summary")

tab1, tab2 = st.tabs(["VIEW", "UPLOAD"])

with tab1: # VIEW
    master_file = "master_pnl.xlsx"
    if os.path.exists(master_file) and os.path.getsize(master_file) > 0:
        mtime = os.path.getmtime(master_file)
        last_updated = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
        col_space, col_btn = st.columns([8, 2])
        with col_btn:
            if st.button("🗑️ 전체 데이터 초기화", type="primary", width="stretch"):
                st.session_state['delete_confirm'] = True
            st.markdown(f"<p style='text-align: right; color: gray; font-size: 0.75rem; margin-top: -10px;'>* 최근 업데이트: {last_updated}</p>", unsafe_allow_html=True)
        
        if st.session_state.get('delete_confirm'):
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ 삭제", width="stretch"):
                    os.remove(master_file); st.session_state['delete_confirm'] = False; st.rerun()
            with c2:
                if st.button("❌ 취소", width="stretch"):
                    st.session_state['delete_confirm'] = False; st.rerun()

        master_df = pd.read_excel(master_file)
        order = ['소매', '도매']
        master_df['소/도매'] = pd.Categorical(master_df['소/도매'], categories=order, ordered=True)

        with st.expander("더존 PL", expanded=True):
            indirect_items = ['원상회복', '연회비', '매도', '낙찰', '위탁', '평가사수수료', '금융수수료', '리본케어','리본케어플러스', '성능보증', '탁송비']
            all_months_numeric = list(range(1, 13)) 
            monthly_data = []

            # 1. 항목별 데이터 수집
            items_to_show = ["상품매출"] + indirect_items
            for i, item in enumerate(items_to_show, start=1):
                if item in master_df.columns:
                    display_name = f"{i:02d}. {item}"
                    for m in all_months_numeric:
                        m_df = master_df[master_df['판매월'] == m]
                        val_total = m_df[item].sum()
                        
                        # 합계/직접/간접 데이터 추가 (구분 이름 고정)
                        monthly_data.append({"항목": display_name, "구분": "0. 합계", "판매월": m, "금액": val_total})
                        if f"{item}_직" in m_df.columns:
                            monthly_data.append({"항목": display_name, "구분": "1. 직접", "판매월": m, "금액": m_df[f"{item}_직"].sum()})
                            monthly_data.append({"항목": display_name, "구분": "2. 간접", "판매월": m, "금액": m_df[f"{item}_간"].sum()})

            # 2. [추가] 00. 총합계 계산 (모든 항목의 0. 합계를 더함)
            total_sum_data = []
            for m in all_months_numeric:
                m_total = master_df[master_df['판매월'] == m][items_to_show].sum().sum()
                monthly_data.append({"항목": "00. 총합계", "구분": " ", "판매월": m, "금액": m_total})

            if monthly_data:
                # 피벗 테이블 생성 및 1~12월 컬럼 강제 유지
                pivot_df = pd.DataFrame(monthly_data).pivot_table(
                    index=["항목", "구분"], columns="판매월", values="금액", aggfunc="sum", fill_value=0
                )
                pivot_df = pivot_df.reindex(columns=all_months_numeric, fill_value=0)
                pivot_df.columns = pivot_df.columns.astype(str)
                
                # 숫자 옆에 ✅/❌를 붙여주는 포맷 함수
                def format_with_status(val, col_name, row_idx):
                    if val == 0: return "-"
                    
                    # 총합계 행은 아이콘 제외, 일반 항목의 '합계' 행만 아이콘 표시
                    if "00. 총합계" not in row_idx[0] and "합계" in row_idx[1]:
                        item_raw = row_idx[0].split(". ")[1]
                        v_col = f"{item_raw}_검증"
                        m_df = master_df[master_df['판매월'] == int(col_name)]
                        if not m_df.empty and v_col in m_df.columns:
                            icon = " ✅" if m_df[v_col].all() else " ❌"
                            return f"{val:,.0f}{icon}"
                    
                    return f"{val:,.0f}"

                # 스타일 함수 (총합계와 각 항목 합계행 강조)
                def apply_row_style(s):
                    is_total_sum = "00. 총합계" in str(s.name[0])
                    is_item_sum = "합계" in str(s.name[1])
                    
                    if is_total_sum:
                        return ['background-color: #e6f3ff; font-weight: bold; border-bottom: 2px solid #004c99'] * len(s)
                    if is_item_sum:
                        return ['background-color: #f8f9fb; font-weight: bold'] * len(s)
                    return [''] * len(s)

                # 데이터 포맷팅 적용
                formatted_df = pivot_df.copy().astype(object)
                for col in pivot_df.columns:
                    for idx in pivot_df.index:
                        formatted_df.loc[idx, col] = format_with_status(pivot_df.loc[idx, col], col, idx)

                st.dataframe(formatted_df.style.apply(apply_row_style, axis=1), width="stretch")
        # --- 나머지 하단 로직 동일 ---
        def style_dataframe(df):
            format_func = lambda x: '-' if x == 0 else f"{x:,.0f}"
            return df.style.format(format_func).set_properties(**{'text-align': 'right', 'font-size': '13px'}) \
                .apply(lambda x: ['background-color: #e6f3ff; font-weight: bold; border-top: 2px solid #004c99' 
                                  if (x.name[0] == '전체' or x.name == '합계(전체)') else '' for _ in x], axis=1)

        if not master_df.empty:
            st.markdown("##### 월별 판매 대수")
            s_p = master_df.pivot_table(index=['상품/위탁', '소/도매'], columns='판매월', values='상품ID', aggfunc='count', fill_value=0).astype(int)
            s_p = s_p.reindex(columns=range(1, 13), fill_value=0)
            s_p['연간 총합'] = s_p.sum(axis=1)
            s_p.loc[('전체', '월별 총합'), :] = s_p.sum(axis=0)
            st.write(style_dataframe(s_p))

            st.markdown("##### 월별 매출")
            rev = master_df.melt(id_vars=['소/도매', '판매월'], value_vars=['상품매출', '용역매출'], var_name='매출항목', value_name='금액')
            r_p = rev.pivot_table(index=['매출항목', '소/도매'], columns='판매월', values='금액', aggfunc='sum', fill_value=0).astype(int)
            r_p = r_p.reindex(columns=range(1, 13), fill_value=0)
            r_p['연간 총합'] = r_p.sum(axis=1)
            r_p.loc[('전체', '합계'), :] = r_p.sum(axis=0)
            st.write(style_dataframe(r_p))

        col1, col2 = st.columns(2)
        with col1: s_yrs = st.multiselect("판매연도", sorted(master_df['판매연도'].unique()), default=sorted(master_df['판매연도'].unique()))
        with col2: s_mths = st.multiselect("판매월", sorted(master_df['판매월'].unique()), default=sorted(master_df['판매월'].unique()))
        d_df = master_df[(master_df['판매연도'].isin(s_yrs)) & (master_df['판매월'].isin(s_mths))]
        st.dataframe(d_df, width="stretch")
        st.download_button("⬇ 데이터 다운로드", to_excel_with_format(d_df, highlight_after_col="판매월"), f"누적데이터_{datetime.now().strftime('%Y%m%d')}.xlsx")
    else:
        st.info("📂 아직 저장된 데이터가 없습니다.")

with tab2: # UPLOAD
    st.header("1️⃣ sales data")
    base_file = st.file_uploader("기준 엑셀 업로드", type=["xlsx"], key="base")
    if base_file:
        base_df = pd.read_excel(base_file)
        base_df["판매일자"] = pd.to_datetime(base_df["판매일자"])
        base_df["판매연도"] = base_df["판매일자"].dt.year
        base_df["판매월"] = base_df["판매일자"].dt.month
        st.dataframe(base_df, width="stretch")

    st.divider()
    st.header("2️⃣ sales by account")
    col_u, col_v = st.columns([7, 3])
    with col_u: u_files = st.file_uploader("매출 엑셀 파일들 업로드", type=["xlsx"], accept_multiple_files=True)
    with col_v: v_file = st.file_uploader("🔍 검증용 엑셀 업로드 (.xls/.xlsx)", type=["xls", "xlsx"])

    if u_files and base_file:
        from processor import preprocess_sales_data
        merged_df = preprocess_sales_data(u_files, base_df)
        st.session_state['merged_df'] = merged_df
        if st.button("▶ 최종 생성"):
            st.session_state['current_final'] = build_final_report(base_df, merged_df)

        if 'current_final' in st.session_state:
            f_df = st.session_state['current_final']
            st.dataframe(f_df, width="stretch")
            if st.button("현재 결과를 마스터 파일에 누적 저장"):
                fname = save_to_master(f_df, verify_file=v_file)
                st.success(f"✅ '{fname}' 누적 저장 및 검증 완료!")