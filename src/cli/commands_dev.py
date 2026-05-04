import typer
from src.cli.base import app, console
from src.skills.xlsx_tools import convert_xlsx_path
from src.skills.pdf_tools import convert_pdf_path
from src.skills.docx_tools import convert_docx_path

@app.command()
def xlsx2md(path: str, recursive: bool = typer.Option(False, "--recursive", "-r")) -> None:
    """将 Excel (xlsx) 转换为 Markdown。"""
    outs, errs = convert_xlsx_path(path, recursive=recursive)
    for o in outs:
        console.print(f"[green]已生成：{o}[/green]")
    for e in errs:
        console.print(f"[yellow]{e}[/yellow]")

@app.command()
def pdf2md(path: str, recursive: bool = typer.Option(False, "--recursive", "-r")) -> None:
    """将 PDF 转换为 Markdown。"""
    outs, errs = convert_pdf_path(path, recursive=recursive)
    for o in outs:
        console.print(f"[green]已生成：{o}[/green]")
    for e in errs:
        console.print(f"[yellow]{e}[/yellow]")

@app.command()
def docx2md(path: str, recursive: bool = typer.Option(False, "--recursive", "-r")) -> None:
    """将 Word (docx) 转换为 Markdown。"""
    outs, errs = convert_docx_path(path, recursive=recursive)
    for o in outs:
        console.print(f"[green]已生成：{o}[/green]")
    for e in errs:
        console.print(f"[yellow]{e}[/yellow]")
