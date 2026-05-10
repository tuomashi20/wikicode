from pathlib import Path

def fix_docstrings(root_dir: Path):
    for p in root_dir.rglob("*.py"):
        if not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8")
            if not content.strip().startswith('"""'):
                print(f"Adding docstring to {p}")
                new_content = f'"""\n{p.name}\n"""\n\n' + content
                p.write_text(new_content, encoding="utf-8")
        except Exception as e:
            print(f"Error processing {p}: {e}")

if __name__ == "__main__":
    fix_docstrings(Path("src"))
