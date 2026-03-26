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
        # [색상 헤더] 매출합계 이후용
        colored_header = workbook.add_format({
            'bg_color': '#DDEBF7',
            'bold': True,
            'border': 1,
            'align': 'center'
        })
        
        # [일반 헤더] 판매주행거리 및 기타 컬럼용 (색상 없음)
        plain_header = workbook.add_format({
            'bold': True,
            'border': 1,
            'align': 'center'
        })
        
        # [데이터] 쉼표 서식
        comma_format = workbook.add_format({'num_format': '#,##0'})

        # 2. 범위 설정
        # 쉼표를 적용할 인덱스 모음
        comma_indices = []
        if '판매주행거리' in df.columns:
            comma_indices.append(df.columns.get_loc('판매주행거리'))
        
        # 색상을 입힐 인덱스 모음 (highlight_after_col 부터 끝까지)
        color_indices = []
        if highlight_after_col and highlight_after_col in df.columns:
            start_idx = df.columns.get_loc(highlight_after_col)
            color_indices = list(range(start_idx, len(df.columns)))
            # 색상 입히는 곳은 당연히 쉼표도 포함
            comma_indices.extend(color_indices)

        # 중복 제거
        comma_indices = set(comma_indices)
        color_indices = set(color_indices)

        # 3. 서식 적용
        for col_num in range(len(df.columns)):
            col_name = df.columns[col_num]
            
            # 기본 너비 설정
            worksheet.set_column(col_num, col_num, 15)

            # --- 헤더 서식 결정 ---
            if col_num in color_indices:
                worksheet.write(0, col_num, col_name, colored_header)
            else:
                worksheet.write(0, col_num, col_name, plain_header)

            # --- 데이터(쉼표) 서식 결정 ---
            if col_num in comma_indices:
                # set_column의 4번째 인자로 서식을 넣으면 해당 열 전체에 적용됩니다.
                worksheet.set_column(col_num, col_num, 15, comma_format)

    output.seek(0)
    return output.getvalue()