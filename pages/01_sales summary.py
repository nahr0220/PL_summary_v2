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
            # 1. 기존 데이터 피벗 (상품/위탁, 소/도매 기준)
            s_p = master_df.pivot_table(index=['상품/위탁', '소/도매'], columns='판매월', values='상품ID', aggfunc='count', fill_value=0, observed=False).astype(int)

            # 2. 월별 컬럼 재색인 및 연간 총합 계산
            s_p = s_p.reindex(columns=range(1, 13), fill_value=0)
            s_p['연간 총합'] = s_p.sum(axis=1)

            # --- 3. 항목별(상품/위탁) 합계를 상단에 추가하는 로직 ---
            # '상품' 그룹 합계, '위탁' 그룹 합계를 각각 계산
            subtotals_s = s_p.groupby(level=0).sum()
            # 정렬 시 상세 내역(소매, 도매)보다 위에 오도록 ' (합계)' 추가 (공백 포함)
            subtotals_s.index = pd.MultiIndex.from_tuples([(x, ' ') for x in subtotals_s.index])
            s_p = pd.concat([s_p, subtotals_s]).sort_index()

            # 4. 전체 총합계 계산 (맨 아래 유지)
            s_p.loc[('전체', '총 판매대수'), :] = subtotals_s.sum(axis=0).values

            # 5. 출력
            st.markdown("""
                <div style="display:flex; justify-content:space-between; align-items:flex-end;">
                    <div style="font-size:20px; font-weight:bold;">월별 판매 대수</div>
                    <div style="font-size:12px; color:gray;">(단위: 대)</div>
                </div>
                """, unsafe_allow_html=True)

            st.write(style_dataframe(s_p))

            # 1. 기존 데이터 처리 (Melt & Pivot)
            rev = master_df.melt(id_vars=['소/도매', '판매월'], value_vars=['상품매출', '용역매출'], var_name='매출항목', value_name='금액')
            r_p = rev.pivot_table(index=['매출항목', '소/도매'], columns='판매월', values='금액', aggfunc='sum', fill_value=0, observed=False).astype(int)

            # 2. 월별 컬럼 재색인 및 연간 총합 계산
            r_p = r_p.reindex(columns=range(1, 13), fill_value=0)
            r_p['연간 총합'] = r_p.sum(axis=1)

            # 3. 항목별(상품/용역) 합계를 상단에 추가하는 로직
            subtotals = r_p.groupby(level=0).sum()
            subtotals.index = pd.MultiIndex.from_tuples([(x, ' ') for x in subtotals.index])
            r_p = pd.concat([r_p, subtotals]).sort_index()

            # 4. 전체 총합계 계산 (맨 아래 유지)
            r_p.loc[('전체', '총 매출액'), :] = subtotals.sum(axis=0).values

            # 최종 출력
            st.markdown("""
                <div style="display:flex; justify-content:space-between; align-items:flex-end;">
                    <div style="font-size:20px; font-weight:bold;">월별 매출</div>
                    <div style="font-size:12px; color:gray;">(단위: 원)</div>
                </div>
                """, unsafe_allow_html=True)

            st.write(style_dataframe(r_p))

        col1, col2 = st.columns(2)
        with col1: s_yrs = st.multiselect("판매연도", sorted(master_df['판매연도'].unique()), default=sorted(master_df['판매연도'].unique()))
        with col2: s_mths = st.multiselect("판매월", sorted(master_df['판매월'].unique()), default=sorted(master_df['판매월'].unique()))
        d_df = master_df[(master_df['판매연도'].isin(s_yrs)) & (master_df['판매월'].isin(s_mths))]
        d_df["판매일자"] = pd.to_datetime(d_df["판매일자"]).dt.date
        # 1. 삭제하고 싶은 후보 리스트
        cols_to_drop = ['고객타입', '사업자유형', '업태', '업종']

        # 2. d_df에 실제로 존재하는 컬럼만 필터링 (이게 핵심!)
        existing_cols = [col for col in cols_to_drop if col in d_df.columns]

        # 3. 존재하는 게 있을 때만 drop 실행
        if existing_cols:
            d_df = d_df.drop(columns=existing_cols)
        display_cols = [col for col in d_df.columns if not col.endswith('_검증')]
        counts = d_df['매입유형1'].value_counts()
        st.markdown(f"**건수:** {len(d_df):,}건 │ **상품매출:** {d_df['상품매출'].sum():,.0f}원 │ **용역매출:** {d_df['용역매출'].sum():,.0f}원 │ **판매월:** {d_df['판매월'].min()}월 ~ {d_df['판매월'].max()}월")
        st.dataframe(d_df[display_cols], width="stretch")
        st.download_button(".xlsx", to_excel_with_format(d_df[display_cols], highlight_after_col="판매연도"), f"sales_summary_{datetime.now().strftime('%Y%m%d')}.xlsx")
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
        base_df["판매일자"] = pd.to_datetime(base_df["판매일자"]).dt.date

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

        # 1. 판매연도, 판매월 필터 (기본 필터링)
        col1, col2 = st.columns(2)
        with col1: 
            sel_year = st.multiselect("판매연도 필터", sorted(merged_df['회계연도'].unique()), default=sorted(merged_df['회계연도'].unique()))
        with col2: 
            sel_month = st.multiselect("판매월 필터", sorted(merged_df['회계월'].unique()), default=sorted(merged_df['회계월'].unique()))
        
        filtered_df = merged_df[merged_df['회계연도'].isin(sel_year) & merged_df['회계월'].isin(sel_month)]
        sel_acc = st.multiselect("계정명 필터", sorted(filtered_df['계정명'].unique()), default=sorted(filtered_df['계정명'].unique()))
        final_df = filtered_df[filtered_df['계정명'].isin(sel_acc)]

        st.dataframe(final_df, width='stretch')
        
        st.download_button(
            label=".xlsx",
            data=to_excel_with_format(final_df, highlight_after_col="판매연도"), # 원본merged_df가 아닌 final_df 전달
            file_name=f"sales_data_by_account_{datetime.now().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        if st.button("3️⃣ 최종 매출 생성", type="primary"):
            st.session_state['current_final'] = build_final_report(base_df, merged_df)

        if 'current_final' in st.session_state:
            f_df = st.session_state['current_final']

            # 현황 요약
            counts = f_df['매입유형1'].value_counts()
            st.markdown(f"**전체:** {len(f_df):,}건 │ **상품:** {len(f_df) - counts.get('위탁', 0):,}건 │ **위탁:** {counts.get('위탁', 0):,}건 │ **매출합계:** {f_df['매출합계'].sum():,.0f}원 │ **판매월:** {f_df['판매월'].min()}월 ~ {f_df['판매월'].max()}월")
            
            def mask_value(value):
                # 값이 없거나 NaN인 경우 빈 문자열 처리
                val_str = str(value).strip() if pd.notna(value) and str(value).strip() != "" else ""
                
                # 2글자 이하면 마스킹하지 않고 그대로 반환
                if len(val_str) <= 2:
                    return val_str
                
                # 앞 2글자 + 나머지 길이만큼 * 반복
                return val_str[:2] + '*' * (len(val_str) - 2)

            # 2. 마스킹을 적용하고 싶은 컬럼 리스트 정의
            target_columns = ['매입처', '정보제공자', '판매처']

            # 3. 반복문을 통해 각 컬럼에 일괄 적용
            for col in target_columns:
                if col in f_df.columns:  # 컬럼이 데이터프레임에 실제 존재하는 경우에만 실행
                    f_df[col] = f_df[col].apply(mask_value)
            
            st.dataframe(f_df, width="stretch")
            col1, col2, _ = st.columns([1, 1, 5]) 

            with col1:
                st.download_button(
                    label=".xlsx", 
                    data=to_excel_with_format(f_df, highlight_after_col="판매연도"), 
                    file_name=f"sales_summary(확인용)_{datetime.now().strftime('%Y%m%d')}.xlsx",
                    width='stretch'
                )

            with col2:
                if st.button("마스터 파일에 저장", width='stretch', type="primary"):
                    fname = save_to_master(f_df, verify_file=v_file)
                    st.success(f"✅ 저장 완료!")
                    st.rerun()