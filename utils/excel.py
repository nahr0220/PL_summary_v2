import io
import pandas as pd

def to_excel_with_format(df, highlight_after_col=None):
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Sheet1')

        workbook  = writer.book
        worksheet = writer.sheets['Sheet1']

        header_format = workbook.add_format({
            'bg_color': '#DDEBF7',
            'bold': True,
            'border': 1,
            'align': 'center'
        })

        if highlight_after_col and highlight_after_col in df.columns:
            start_col = df.columns.get_loc(highlight_after_col) + 1
            for col_num in range(start_col, len(df.columns)):
                col_name = df.columns[col_num]
                worksheet.write(0, col_num, col_name, header_format)
                worksheet.set_column(col_num, col_num, 15)

    output.seek(0)
    return output.getvalue()
