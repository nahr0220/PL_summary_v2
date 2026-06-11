import streamlit as st
import pandas as pd
import numpy as np
from utils.excel import to_excel_with_format
from processor import preprocess_sales_data
from analyzer import build_final_report, save_to_master, enrich_vendor_hr, compute_verification
from datetime import datetime
from zoneinfo import ZoneInfo
import os

def mask_value(value):
    # 값이 없거나 NaN인 경우 빈 문자열 처리
    val_str = str(value).strip() if pd.notna(value) and str(value).strip() != "" else ""
    
    # 2글자 이하면 그대로 반환
    if len(val_str) <= 2:
        return val_str
    
    # 앞 2글자 + '*' 하나만 (개인정보 보호용)
    return val_str[:2] + '*'

def load_mapping_file(uploaded, master_path, required_cols, label):
    """업로드된 매핑파일이 있으면 검증 후 저장하고 반환, 없으면 기존 저장본 사용."""
    if uploaded is not None:
        m_df = pd.read_excel(uploaded)
        missing = [c for c in required_cols if c not in m_df.columns]
        if missing:
            st.error(f"❌ {label} 필수 컬럼 누락: {', '.join(missing)}")
            return None
        m_df.to_excel(master_path, index=False)  # 다음 실행 재사용을 위해 저장
        return m_df
    if os.path.exists(master_path):
        return pd.read_excel(master_path)
    return None

HR_COLUMN_NAMES = ['결산월', '관리용 코드', '대표이사', '본부', '실', '팀', '파트', '팀명', '매입구분', '구분자', '도소매구분', '관리명', '예외']

def load_hr_file(uploaded, master_path):
    """인사정보 파일: header=1, 앞 13개 컬럼만 사용하고 지정 컬럼명으로 맞춘다.
    업로드 시 정리된 형태로 저장(이후 fallback은 정리본을 header=0으로 읽음)."""
    if uploaded is not None:
        raw = pd.read_excel(uploaded, header=1)
        raw = raw.iloc[:, :13]
        raw.columns = HR_COLUMN_NAMES[:raw.shape[1]]
        need = ['결산월', '팀', '본부', '실', '관리명']
        missing = [c for c in need if c not in raw.columns]
        if missing:
            st.error(f"❌ 인사정보 필수 컬럼 누락 (header=1·13열 확인): {', '.join(missing)}")
            return None
        raw.to_excel(master_path, index=False)
        return raw
    if os.path.exists(master_path):
        return pd.read_excel(master_path)
    return None

def render_douzone_pl(source_df, key_prefix="pl"):
    """더존 PL 피벗 표시. {item}_검증 컬럼이 있으면(=저장 후) ✅/❌ 아이콘 표시,
    {item}_검증값이 있으면 PL계산 vs 검증파일 + 차액 상세표도 표시."""
    indirect_items = ['원상회복', '연회비', '매도', '낙찰', '위탁', '평가사수수료', '금융수수료', '리본케어', '리본케어플러스', '성능보증', '탁송비']
    all_months_numeric = list(range(1, 13))
    items_to_show = ["상품매출"] + indirect_items
    monthly_data = []
    for i, item in enumerate(items_to_show, start=1):
        if item in source_df.columns:
            display_name = f"{i:02d}. {item}"
            for m in all_months_numeric:
                m_df = source_df[source_df['판매월'] == m]
                monthly_data.append({"항목": display_name, "구분": "0. 합계", "판매월": m, "금액": m_df[item].sum()})
                if f"{item}_직" in m_df.columns:
                    monthly_data.append({"항목": display_name, "구분": "1. 직접", "판매월": m, "금액": m_df[f"{item}_직"].sum()})
                if f"{item}_간" in m_df.columns:
                    monthly_data.append({"항목": display_name, "구분": "2. 간접", "판매월": m, "금액": m_df[f"{item}_간"].sum()})
    for m in all_months_numeric:
        m_total = source_df[source_df['판매월'] == m][items_to_show].sum().sum()
        monthly_data.append({"항목": "00. 총합계", "구분": " ", "판매월": m, "금액": m_total})

    if not monthly_data:
        return

    pivot_df = pd.DataFrame(monthly_data).pivot_table(
        index=["항목", "구분"], columns="판매월", values="금액",
        aggfunc="sum", fill_value=0, observed=False
    )
    pivot_df = pivot_df.reindex(columns=all_months_numeric, fill_value=0)
    pivot_df['연간'] = pivot_df.sum(axis=1)
    pivot_df.columns = pivot_df.columns.astype(str)

    def format_with_status(val, col_name, row_idx):
        if "00. 총합계" not in row_idx[0] and "합계" in row_idx[1]:
            item_raw = row_idx[0].split(". ")[1]
            v_col = f"{item_raw}_검증"
            if v_col in source_df.columns:  # 저장 후에만 검증 아이콘
                if col_name == '연간':
                    is_ok = source_df[v_col].all()
                else:
                    m_df = source_df[source_df['판매월'] == int(col_name)]
                    is_ok = m_df[v_col].all() if not m_df.empty else True
                icon = " ✅" if is_ok else " ❌"
                if val == 0:
                    return f"0{icon}" if not is_ok else "-"
                return f"{val:,.0f}{icon}"
        return "-" if val == 0 else f"{val:,.0f}"

    def apply_row_style(s):
        if "00. 총합계" in str(s.name[0]):
            return ['background-color: #e6f3ff; font-weight: bold; border-bottom: 2px solid #004c99'] * len(s)
        if "합계" in str(s.name[1]):
            return ['background-color: #f8f9fb; font-weight: bold'] * len(s)
        return [''] * len(s)

    def apply_col_style(s):
        if s.name == '연간':
            return ['font-weight: bold; border-left: 2px solid #f9a825'] * len(s)
        return [''] * len(s)

    formatted_df = pivot_df.copy().astype(object)
    for col in pivot_df.columns:
        for idx in pivot_df.index:
            formatted_df.loc[idx, col] = format_with_status(pivot_df.loc[idx, col], col, idx)

    st.dataframe(
        formatted_df.style.apply(apply_row_style, axis=1).apply(apply_col_style, axis=0),
        use_container_width=True
    )

    # 검증 상세: PL 계산값 vs 검증파일값 + 차액 ({item}_검증값 컬럼이 있을 때만 = 저장 후)
    detail_rows = []
    for item in items_to_show:
        vcol = f"{item}_검증값"
        if item in source_df.columns and vcol in source_df.columns:
            for m in all_months_numeric:
                m_df = source_df[source_df['판매월'] == m]
                if m_df.empty:
                    continue
                actual_series = m_df[vcol].dropna()
                if actual_series.empty:
                    continue  # 해당 월 검증파일 값 없음
                actual = actual_series.iloc[0]
                calc = m_df[item].sum()
                diff = calc - actual
                detail_rows.append({
                    "항목": item, "월": m,
                    "PL계산": calc, "검증파일": actual, "차액": diff,
                    "일치": "✅" if round(diff) == 0 else "❌",
                })

    if detail_rows:
        detail_df = pd.DataFrame(detail_rows).sort_values(["일치", "항목", "월"]).reset_index(drop=True)
        st.markdown("**검증 상세 (PL 계산 vs 검증파일)**")
        only_diff = st.checkbox("차액 있는 항목만 보기", value=True, key=f"{key_prefix}_diff_only")
        view_df = detail_df[detail_df["일치"] == "❌"] if only_diff else detail_df
        if view_df.empty:
            st.caption("차액이 있는 항목이 없습니다. (전부 정확히 일치)")
        else:
            st.dataframe(
                view_df.style
                    .format({"PL계산": "{:,.0f}", "검증파일": "{:,.0f}", "차액": "{:,.0f}"})
                    .apply(lambda r: ['background-color:#fdecea' if r['일치'] == '❌' else '' for _ in r], axis=1),
                use_container_width=True, hide_index=True
            )

st.set_page_config(page_title="손익분석", layout="wide")
st.title("Sales Summary")

tab1, tab2 = st.tabs(["VIEW", "UPLOAD"])

with tab1:  # VIEW (매출요약정보)
    master_file = "master_pnl.xlsx"
    if os.path.exists(master_file) and os.path.getsize(master_file) > 0:
        
        master_df = pd.read_excel(master_file)

        # ✅ 최근 업데이트 시간 (데이터 내 컬럼 혹은 파일 시스템의 수정 시간 기준)
        try:
            # 'updated_at' 컬럼이 있고, 비어있지 않은 경우
            if 'updated_at' in master_df.columns and not master_df['updated_at'].isna().all():
                last_updated = pd.to_datetime(master_df['updated_at']).max().strftime('%Y-%m-%d %H:%M:%S')
            else:
                raise ValueError("updated_at column is empty or missing valid data") # 강제로 except 블록으로 이동
        except Exception: # 컬럼 누락, 데이터 오류 등 모든 예외 상황에서 파일 시스템 시간으로 대체
            # 컬럼이 없거나 에러 시 파일의 실제 수정 시간 표시
            mtime = os.path.getmtime(master_file)
            last_updated = datetime.fromtimestamp(mtime, tz=ZoneInfo("Asia/Seoul")).strftime('%Y-%m-%d %H:%M:%S')

        col_space, col_btn = st.columns([8, 2])
        with col_btn:
            if st.button("🗑️ 전체 데이터 초기화", type="primary", use_container_width=True):
                st.session_state['delete_confirm'] = True

            st.markdown(
                f"<p style='text-align: right; color: gray; font-size: 0.75rem; margin-top: -10px;'>* 최근 업데이트: {last_updated}</p>",
                unsafe_allow_html=True
            )

        if st.session_state.get('delete_confirm'):
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ 삭제", use_container_width=True):
                    os.remove(master_file)
                    st.session_state['delete_confirm'] = False
                    st.rerun()
            with c2:
                if st.button("❌ 취소", use_container_width=True):
                    st.session_state['delete_confirm'] = False
                    st.rerun()
                
        order = ['소매', '도매']
        master_df['소/도매'] = pd.Categorical(master_df['소/도매'], categories=order, ordered=True)

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

            st.dataframe(style_dataframe(s_p), use_container_width=True)

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

            st.dataframe(style_dataframe(r_p), use_container_width=True)

        head_v_l, head_v_r = st.columns([8, 2])
        with head_v_l:
            st.markdown('<div style="font-size:20px; font-weight:bold;">차량별 매출현황</div>', unsafe_allow_html=True)
        dl_slot_view = head_v_r.empty()  # 제목 우측 엑셀 다운로드 버튼 자리

        col1, col2 = st.columns(2)
        with col1: s_yrs = st.multiselect("판매연도", sorted(master_df['판매연도'].unique()), default=sorted(master_df['판매연도'].unique()))
        with col2: s_mths = st.multiselect("판매월", sorted(master_df['판매월'].unique()), default=sorted(master_df['판매월'].unique()))
        d_df = master_df[(master_df['판매연도'].isin(s_yrs)) & (master_df['판매월'].isin(s_mths))]
        d_df["판매일자"] = pd.to_datetime(d_df["판매일자"]).dt.date

        # ✅ 거래처(매입처 구분)·인사정보는 UPLOAD 저장 시점에 master에 결합되므로 여기서는 표시만 함

        # ✅ 화면 표시 직전에 마스킹 적용 (매핑이 완료된 원본 데이터를 사용한 후 변조)
        target_mask_cols = ['매입처', '정보제공자', '판매처']
        for col in target_mask_cols:
            if col in d_df.columns:
                d_df[col] = d_df[col].apply(mask_value)

        d_df['>>컬럼구분>>'] = ''

        # 매입본부/매입실/매입팀/매입구분/매입담당, 판매팀/판매실/판매담당 등은
        # UPLOAD 저장 시점에 인사정보가 결합되어 master에 이미 포함되어 있음 (표시만)

        # 1. 삭제하고 싶은 후보 리스트
        cols_to_drop = ['고객타입', '사업자유형', '업태', '업종']

        # 2. d_df에 실제로 존재하는 컬럼만 필터링 (이게 핵심!)
        existing_cols = [col for col in cols_to_drop if col in d_df.columns]

        # 3. 존재하는 게 있을 때만 drop 실행
        if existing_cols:
            d_df = d_df.drop(columns=existing_cols)
        display_cols = [col for col in d_df.columns if '_검증' not in col]
        counts = d_df['매입유형1'].value_counts()
        st.markdown(f"**대수:** {len(d_df):,}대 │ **상품매출:** {d_df['상품매출'].sum():,.0f}원 │ **용역매출:** {d_df['용역매출'].sum():,.0f}원 │ **판매월:** {d_df['판매월'].min()}월 ~ {d_df['판매월'].max()}월")
        st.dataframe(d_df[display_cols], use_container_width=True)
        dl_slot_view.download_button(
            "엑셀 다운로드",
            to_excel_with_format(d_df[display_cols], highlight_after_col=">>컬럼구분>>"),
            f"sales_summary_{datetime.now().strftime('%Y%m%d')}.xlsx",
            use_container_width=True
        )

        # 하단에 더존 PL 표시
        st.divider()
        with st.expander("더존 PL(단위:원)", expanded=False):
            render_douzone_pl(master_df, key_prefix="view")

    else:
        st.info("📂 아직 저장된 데이터가 없습니다.")

with tab2: # UPLOAD
    st.markdown('<div style="font-size:20px; font-weight:bold;">판매차량</div>', unsafe_allow_html=True)
    base_file = st.file_uploader("업로드 파일 ㅣ 총 1개 파일 ㅣ 손익분석", type=["xlsx"], key="base")
    if base_file:
        base_df = pd.read_excel(base_file)
        base_df["판매일자"] = pd.to_datetime(base_df["판매일자"])
        base_df["판매연도"] = base_df["판매일자"].dt.year
        base_df["판매월"] = base_df["판매일자"].dt.month
        cols = [c for c in base_df.columns if c != '판매월'] + ['판매월']
        base_df = base_df[cols]
        base_df["판매일자"] = pd.to_datetime(base_df["판매일자"]).dt.date

        total_cnt = len(base_df)
        consign_cnt = (base_df['매입유형1'] == '위탁').sum()
        product_cnt = total_cnt - consign_cnt
        st.markdown(f"**전체:** {total_cnt:,}대 │ **상품:** {product_cnt:,}대 │ **위탁:** {consign_cnt:,}대 │ **판매월:** {base_df['판매월'].min()}월 ~ {base_df['판매월'].max()}월")
        st.dataframe(base_df, use_container_width=True)

    st.divider()
    head2_l, head2_r = st.columns([8, 2])
    with head2_l:
        st.markdown('<div style="font-size:20px; font-weight:bold;">계정별원장(기간별손익계산서)</div>', unsafe_allow_html=True)
    dl_slot_account = head2_r.empty()  # 제목 우측 엑셀 다운로드 버튼 자리
    col_u, col_v = st.columns(2)
    with col_u: u_files = st.file_uploader("업로드 파일 ㅣ 총 12개 파일 ㅣ 상품매출(자동차), 수입수수료(반납취소, 정보제공, 상품화, 잔가옵션 제외)", type=["xlsx"], accept_multiple_files=True)
    with col_v: v_file = st.file_uploader("업로드 파일 ㅣ 기간별손익계산서", type=["xls", "xlsx"])

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

        # 💡 필터링된 데이터 요약 한 줄 추가
        st.markdown(f"**선택된 데이터:** {len(final_df):,}건 │ **대변 합계:** {final_df['대변'].sum():,.0f}원")
        st.dataframe(final_df, use_container_width=True)

        dl_slot_account.download_button(
            label="엑셀 다운로드",
            data=to_excel_with_format(final_df, highlight_after_col="판매연도"), # 원본merged_df가 아닌 final_df 전달
            file_name=f"sales_data_by_account_{datetime.now().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

        st.divider()

        # 기준정보 (계정별원장 데이터 이후 표시) — 차량별 매출현황 계산 전에 vendor/hr 준비
        st.markdown('<div style="font-size:20px; font-weight:bold;">기준정보</div>', unsafe_allow_html=True)
        col_vendor, col_hr = st.columns(2)
        with col_vendor:
            up_vendor = st.file_uploader("거래처", type=["xlsx"], key="vendor_up")
        with col_hr:
            up_hr = st.file_uploader("인사정보", type=["xlsx"], key="hr_up")

        # 업로드 시 master_vendor/master_hr 로 저장하고, 없으면 기존 저장본 사용
        vendor_df = load_mapping_file(up_vendor, "master_vendor.xlsx", ["거래처", "거래처_정정"], "거래처")
        hr_df = load_hr_file(up_hr, "master_hr.xlsx")

        with st.expander("현재 적용 중인 기준정보 보기", expanded=False):
            c1, c2 = st.columns(2)
            with c1:
                st.write("**거래처**")
                if vendor_df is not None:
                    st.dataframe(vendor_df.rename(columns={'거래처_정정': '매입처 구분'}), use_container_width=True, hide_index=True)
                else:
                    st.caption("등록된 거래처 매핑이 없습니다.")
            with c2:
                st.write("**인사정보**")
                if hr_df is not None:
                    st.dataframe(hr_df, use_container_width=True, hide_index=True)
                else:
                    st.caption("등록된 인사정보가 없습니다.")

        st.divider()

        head3_l, head3_r = st.columns([8, 2])
        with head3_l:
            st.markdown('<div style="font-size:20px; font-weight:bold;">차량별 매출현황</div>', unsafe_allow_html=True)
        dl_slot_final = head3_r.empty()  # 제목 우측 엑셀 다운로드 버튼 자리

        if st.button("차량별 매출현황 계산", type="primary"):
            f_df = build_final_report(base_df, merged_df)
            # 거래처(매입처 구분) + 인사정보(판매연도/판매월 = 결산연월 매칭) 결합
            f_df = enrich_vendor_hr(f_df, vendor_df, hr_df)
            st.session_state['current_final'] = f_df

        if 'current_final' in st.session_state:
            f_df = st.session_state['current_final']

            # 현황 요약
            counts = f_df['매입유형1'].value_counts()
            st.markdown(f"**전체:** {len(f_df):,}대 │ **상품:** {len(f_df) - counts.get('위탁', 0):,}대 │ **위탁:** {counts.get('위탁', 0):,}대 │ **매출합계:** {f_df['매출합계'].sum():,.0f}원 │ **판매월:** {f_df['판매월'].min()}월 ~ {f_df['판매월'].max()}월")

            st.dataframe(f_df, use_container_width=True)

            dl_slot_final.download_button(
                label="엑셀 다운로드",
                data=to_excel_with_format(f_df, highlight_after_col=">>컬럼구분>>"),
                file_name=f"sales_summary(확인용)_{datetime.now().strftime('%Y%m%d')}.xlsx",
                use_container_width=True
            )

            # 마스터 저장 전 더존 PL 미리보기 (검증파일 올리면 VIEW와 동일하게 ✅/❌·차액까지)
            with st.expander("더존 PL 미리보기 (저장 전 · 단위:원)", expanded=True):
                if v_file is not None:
                    preview_df, v_err = compute_verification(f_df, v_file)
                    if v_err:
                        st.caption(f"⚠️ 검증 파일 처리 문제: {v_err}")
                    render_douzone_pl(preview_df, key_prefix="upload")
                else:
                    st.caption("검증용 파일(기간별손익계산서)을 올리면 ✅/❌·차액까지 표시됩니다.")
                    render_douzone_pl(f_df, key_prefix="upload")

            col_save, _ = st.columns([2, 8])
            with col_save:
                if st.button("VIEW 반영", use_container_width=True, type="primary"):
                    fname, v_err = save_to_master(f_df, verify_file=v_file)
                    if v_err:
                        st.warning(f"⚠️ 저장되었으나 검증은 스킵되었습니다.\n사유: {v_err}")
                        if "xlrd" in v_err:
                            st.info("Tip: 터미널에서 'pip install xlrd'를 실행해 주세요.")
                    else:
                        st.success(f"✅ 저장 및 검증 완료!")
                    # st.rerun() # 메시지를 확인하기 위해 필요한 경우 주석 처리하거나 적절한 타이밍에 실행