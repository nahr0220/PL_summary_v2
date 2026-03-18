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

        with st.expander("더존 PL(단위:원)", expanded=True):
            indirect_items = ['원상회복', '연회비', '매도', '낙찰', '위탁', '평가사수수료', '금융수수료', '리본케어','리본케어플러스', '성능보증', '탁송비']
            all_months_numeric = list(range(1, 13)) 
            monthly_data = []

            items_to_show = ["상품매출"] + indirect_items
            for i, item in enumerate(items_to_show, start=1):
                if item in master_df.columns:
                    display_name = f"{i:02d}. {item}"
                    for m in all_months_numeric:
                        m_df = master_df[master_df['판매월'] == m]
                        val_total = m_df[item].sum()
                        
                        monthly_data.append({"항목": display_name, "구분": "0. 합계", "판매월": m, "금액": val_total})
                        if f"{item}_직" in m_df.columns:
                            monthly_data.append({"항목": display_name, "구분": "1. 직접", "판매월": m, "금액": m_df[f"{item}_직"].sum()})
                        if f"{item}_간" in m_df.columns:
                            monthly_data.append({"항목": display_name, "구분": "2. 간접", "판매월": m, "금액": m_df[f"{item}_간"].sum()})

            # 00. 총합계 계산 (VIEW 섹션 상단 노출용)
            for m in all_months_numeric:
                m_total = master_df[master_df['판매월'] == m][items_to_show].sum().sum()
                monthly_data.append({"항목": "00. 총합계", "구분": " ", "판매월": m, "금액": m_total})

            if monthly_data:
                pivot_df = pd.DataFrame(monthly_data).pivot_table(
                    index=["항목", "구분"], columns="판매월", values="금액", aggfunc="sum", fill_value=0, observed=False
                )
                pivot_df = pivot_df.reindex(columns=all_months_numeric, fill_value=0)
                pivot_df.columns = pivot_df.columns.astype(str)
                
                def format_with_status(val, col_name, row_idx):
                    if val == 0: return "-"
                    if "00. 총합계" not in row_idx[0] and "합계" in row_idx[1]:
                        item_raw = row_idx[0].split(". ")[1]
                        v_col = f"{item_raw}_검증"
                        m_df = master_df[master_df['판매월'] == int(col_name)]
                        if not m_df.empty and v_col in m_df.columns:
                            icon = " ✅" if m_df[v_col].all() else " ❌"
                            return f"{val:,.0f}{icon}"
                    return f"{val:,.0f}"

                def apply_row_style(s):
                    if "00. 총합계" in str(s.name[0]):
                        return ['background-color: #e6f3ff; font-weight: bold; border-bottom: 2px solid #004c99'] * len(s)
                    if "합계" in str(s.name[1]):
                        return ['background-color: #f8f9fb; font-weight: bold'] * len(s)
                    return [''] * len(s)

                formatted_df = pivot_df.copy().astype(object)
                for col in pivot_df.columns:
                    for idx in pivot_df.index:
                        formatted_df.loc[idx, col] = format_with_status(pivot_df.loc[idx, col], col, idx)

                st.dataframe(formatted_df.style.apply(apply_row_style, axis=1), width="stretch")

        def style_dataframe(df):
            return df.style.format(lambda x: '-' if x == 0 else f"{x:,.0f}").set_properties(**{'text-align': 'right', 'font-size': '13px'}) \
                .apply(lambda x: ['background-color: #e6f3ff; font-weight: bold; border-top: 2px solid #004c99' 
                                  if (x.name[0] == '전체' or x.name == '합계(전체)') else '' for _ in x], axis=1)

        if not master_df.empty:
            st.markdown("""
            <div style="display:flex; justify-content:space-between; align-items:flex-end;">
                <div style="font-size:20px; font-weight:bold;">
                    월별 판매 대수
                </div>
                <div style="font-size:12px; color:gray;">
                    (단위: 대)
                </div>
            </div>
            """, unsafe_allow_html=True)
            s_p = master_df.pivot_table(index=['상품/위탁', '소/도매'], columns='판매월', values='상품ID', aggfunc='count', fill_value=0, observed=False).astype(int)
            s_p = s_p.reindex(columns=range(1, 13), fill_value=0)
            s_p['연간 총합'] = s_p.sum(axis=1)
            s_p.loc[('전체', '월별 총합'), :] = s_p.sum(axis=0)
            st.write(style_dataframe(s_p))

            st.markdown("""
            <div style="display:flex; justify-content:space-between; align-items:flex-end;">
                <div style="font-size:20px; font-weight:bold;">
                    월별 매출
                </div>
                <div style="font-size:12px; color:gray;">
                    (단위: 원)
                </div>
            </div>
            """, unsafe_allow_html=True)
            rev = master_df.melt(id_vars=['소/도매', '판매월'], value_vars=['상품매출', '용역매출'], var_name='매출항목', value_name='금액')
            r_p = rev.pivot_table(index=['매출항목', '소/도매'], columns='판매월', values='금액', aggfunc='sum', fill_value=0, observed=False).astype(int)
            r_p = r_p.reindex(columns=range(1, 13), fill_value=0)
            r_p['연간 총합'] = r_p.sum(axis=1)
            r_p.loc[('전체', '합계'), :] = r_p.sum(axis=0)
            st.write(style_dataframe(r_p))

        col1, col2 = st.columns(2)
        with col1: s_yrs = st.multiselect("판매연도", sorted(master_df['판매연도'].unique()), default=sorted(master_df['판매연도'].unique()))
        with col2: s_mths = st.multiselect("판매월", sorted(master_df['판매월'].unique()), default=sorted(master_df['판매월'].unique()))
        d_df = master_df[(master_df['판매연도'].isin(s_yrs)) & (master_df['판매월'].isin(s_mths))]
        display_cols = [col for col in d_df.columns if not col.endswith('_검증')]
        counts = d_df['매입유형1'].value_counts()
        st.markdown(f"**건수:** {len(d_df):,}건 │ **상품:** {len(d_df) - counts.get('위탁', 0):,}건 │ **위탁:** {counts.get('위탁', 0):,}건 │ **매출합계:** {d_df['매출합계'].sum():,.0f}원 │ **판매월:** {d_df['판매월'].min()}월 ~ {d_df['판매월'].max()}월")
        st.dataframe(d_df[display_cols], width="stretch")
        st.download_button("⬇ 데이터 다운로드", to_excel_with_format(d_df[display_cols], highlight_after_col="판매월"), f"누적데이터_{datetime.now().strftime('%Y%m%d')}.xlsx")
    else:
        st.info("📂 아직 저장된 데이터가 없습니다.")

with tab2: # UPLOAD
    st.header("1️⃣ sales data")
    base_file = st.file_uploader("기준 파일 업로드", type=["xlsx"], key="base")
    if base_file:
        base_df = pd.read_excel(base_file)
        base_df["판매일자"] = pd.to_datetime(base_df["판매일자"])
        base_df["판매연도"] = base_df["판매일자"].dt.year
        base_df["판매월"] = base_df["판매일자"].dt.month
        cols = [c for c in base_df.columns if c != '판매월'] + ['판매월']
        base_df = base_df[cols]

        st.success("기준 데이터 로드 완료")
        total_cnt = len(base_df)
        consign_cnt = (base_df['매입유형1'] == '위탁').sum()
        product_cnt = total_cnt - consign_cnt
        st.markdown(f"**전체:** {total_cnt:,}건 │ **상품:** {product_cnt:,}건 │ **위탁:** {consign_cnt:,}건 │ **판매월:** {base_df['판매월'].min()}월 ~ {base_df['판매월'].max()}월")
        st.dataframe(base_df, width="stretch")

    st.divider()
    st.header("2️⃣ sales by account")
    col_u, col_v = st.columns([7, 3])
    with col_u: u_files = st.file_uploader("매출 파일 업로드", type=["xlsx"], accept_multiple_files=True)
    with col_v: v_file = st.file_uploader("검증용 더존 업로드 (.xls/.xlsx)", type=["xls", "xlsx"])

    if u_files and base_file:
        merged_df = preprocess_sales_data(u_files, base_df)
        st.session_state['merged_df'] = merged_df

        sel_acc = st.multiselect("계정명 필터", sorted(merged_df['계정명'].unique()), default=sorted(merged_df['계정명'].unique()))
        st.dataframe(merged_df[merged_df['계정명'].isin(sel_acc)], width="stretch")

        if st.button("3️⃣ 최종 매출 생성", type="primary"):
            st.session_state['current_final'] = build_final_report(base_df, merged_df)

        if 'current_final' in st.session_state:
            f_df = st.session_state['current_final']
            st.dataframe(f_df, width="stretch")
            if st.button("현재 결과를 마스터 파일에 누적 저장"):
                fname = save_to_master(f_df, verify_file=v_file)
                st.success(f"✅ '{fname}' 누적 저장 및 검증 완료!")
                st.rerun()