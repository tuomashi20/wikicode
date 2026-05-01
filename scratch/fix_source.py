import os
path = r'D:\project\wikicode\src\core\build_agent.py'
content = open(path, 'r', encoding='utf-8').read()

old_marker = '    - 【执行效率】：'
# Find the start and end based on line structure
start_idx = content.find(old_marker)
if start_idx != -1:
    # Find the end of the "破除模板" line
    end_marker = '破除模板**：必须立即改写首页，展现真实业务逻辑。'
    end_idx = content.find(end_marker) + len(end_marker)
    
    new_part = """    - 【执行效率】：
      - **【极速交付】**：
        - **文件识别**：严禁对带后缀名的文件使用 `mkdir`！
        - **兜底逻辑**：如果发现 `package.json` 缺失，必须立即使用 `create_file` 手动补齐！严禁在没有 package.json 的情况下尝试 `npm install`。
        - **清场重试**：如果官方脚手架（如 `create-next-app`）因为目录非空报错，请先清空目录再重新运行。
        - **步骤硬化**：第 1 步脚手架，第 2 步必须重写首页。
        - **交付核验**：在 `finish` 前，自检根目录是否存在 `package.json`、`src/app/page.tsx`。"""
    
    new_content = content[:start_idx] + new_part + content[end_idx:]
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("SUCCESS")
else:
    print("FAILED")
