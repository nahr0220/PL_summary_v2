import io
import pandas as pd

import io
import pandas as pd

def to_excel_with_format(df, highlight_after_col=None):
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Sheet1')

        workbook  = writer.book
        worksheet = writer.sheets['Sheet1']

        # 1. 서식 정의
        colored_header = workbook.add_format({
            'bg_color': '#DDEBF7',
            'bold': True,
            'border': 1,
            'align': 'center'
        })
        
        plain_header = workbook.add_format({
            'bold': True,
            'border': 1,
            'align': 'center'
        })
        
        comma_format = workbook.add_format({'num_format': '#,##0'})

        # 2. 범위 설정
        comma_indices = []
        if '판매주행거리' in df.columns:
            comma_indices.append(df.columns.get_loc('판매주행거리'))
        
        color_indices = []
        if highlight_after_col and highlight_after_col in df.columns:
            start_idx = df.columns.get_loc(highlight_after_col)
            color_indices = list(range(start_idx, len(df.columns)))
            comma_indices.extend(color_indices)

        # [핵심 수정] 판매연도가 리스트에 있다면 쉼표 대상에서 제외하기
        if '판매연도' in df.columns:
            year_idx = df.columns.get_loc('판매연도')
            if year_idx in comma_indices:
                comma_indices = [i for i in comma_indices if i != year_idx]

        # 중복 제거
        comma_indices = set(comma_indices)
        color_indices = set(color_indices)

        # 3. 서식 적용
        for col_num in range(len(df.columns)):
            col_name = df.columns[col_num]
            worksheet.set_column(col_num, col_num, 15)

            # --- 헤더 서식 결정 ---
            if col_num in color_indices:
                worksheet.write(0, col_num, col_name, colored_header)
            else:
                worksheet.write(0, col_num, col_name, plain_header)

            # --- 데이터(쉼표) 서식 결정 ---
            if col_num in comma_indices:
                worksheet.set_column(col_num, col_num, 15, comma_format)
            else:
                # 쉼표가 없는 열도 기본적으로 숫자가 들어있다면 
                # 서식 없이 열 너비만 유지하도록 설정
                worksheet.set_column(col_num, col_num, 15)

    output.seek(0)
    return output.getvalue()