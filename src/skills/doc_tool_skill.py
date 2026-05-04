"""
doc_tool_skill.py - 文档格式转换 Skill (XLSX, PDF, DOCX -> MD)。
"""
from pathlib import Path
from typing import Optional

def convert_xlsx_to_md(input_path: str, output_path: Optional[str] = None) -> str:
    """将 Excel 转换为 Markdown"""
    from src.skills.xlsx_tools import xlsx_to_markdown
    return xlsx_to_markdown(input_path, output_path)

def convert_pdf_to_md(input_path: str, output_path: Optional[str] = None) -> str:
    """将 PDF 转换为 Markdown"""
    from src.skills.pdf_tools import pdf_to_markdown
    return pdf_to_markdown(input_path, output_path)

def convert_docx_to_md(input_path: str, output_path: Optional[str] = None) -> str:
    """将 Word 转换为 Markdown"""
    from src.skills.docx_tools import docx_to_markdown
    return docx_to_markdown(input_path, output_path)
