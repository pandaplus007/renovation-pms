"""Excel 导出公共工具。"""
from io import BytesIO
from urllib.parse import quote

from fastapi.responses import Response
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill


def export_xlsx(filename: str, headers: list[str], rows: list[list],
                sheet_name: str = "导出", col_widths: dict | None = None) -> Response:
    """生成带表头的 xlsx 下载响应。

    Args:
        filename: 下载文件名（可含中文，自动 URL 编码）
        headers: 表头列表
        rows: 数据行（每行与 headers 等长）
        sheet_name: 工作表名
        col_widths: 可选，{列序号(1起): 宽度} 覆盖自动宽度
    """
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31] or "导出"

    ws.append(headers)
    fill = PatternFill("solid", fgColor="2563EB")
    font = Font(color="FFFFFF", bold=True)
    for cell in ws[1]:
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row in rows:
        ws.append(["" if v is None else v for v in row])

    # 自动列宽（中文按 2 字符宽估算）
    for col_idx in range(1, len(headers) + 1):
        if col_widths and col_idx in col_widths:
            width = col_widths[col_idx]
        else:
            width = 10
            for cell in ws.iter_cols(min_col=col_idx, max_col=col_idx):
                for c in cell:
                    if c.value is not None:
                        text_len = sum(2 if ord(ch) > 127 else 1 for ch in str(c.value))
                        width = max(width, text_len + 2)
            width = min(width, 45)
        ws.column_dimensions[chr(64 + col_idx)].width = width

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )
