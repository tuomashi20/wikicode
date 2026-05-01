import os
import sys
import json
from pathlib import Path

# 将项目根目录加入 sys.path
PROJECT_ROOT = Path("D:/project/wikicode")
sys.path.append(str(PROJECT_ROOT))

from src.utils.config import load_config
from src.core.build_agent import BuildAgent

import io
import urllib.request
import time

# 强制设置标准输出编码为 UTF-8，解决 Windows 下의 GBK 冲突
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def test_pet_adoption_platform():
    config = load_config()
    test_dir = "D:/project/test_final_v29"
    
    # 彻底清理旧环境
    import shutil
    if os.path.exists(test_dir):
        try:
            shutil.rmtree(test_dir, ignore_errors=True)
        except: pass
    os.makedirs(test_dir, exist_ok=True)
    
    agent = BuildAgent(config, cwd=test_dir)
    
    prompt = """项目:宠物领养平台
## 项目概述
开发一个现代化的宠物领养平台MVP,连接待领养宠物与潜在领养者,提供流畅的领养申请流程。
## 技术栈
- 前端框架: Next.js 14+ (App Router)
- 样式方案: Tailwind CSS 3.x
- 后端/数据库: Supabase (PostgreSQL + Auth + Storage + Realtime)

## 核心功能需求
### 用户系统
- 用户注册/登录 (邮箱、Google OAuth)
- 用户角色: 普通用户、宠物发布者、管理员
- 个人资料管理

### 宠物展示
- 宠物列表页
- 宠物详情页
- 搜索功能 (支持筛选: 种类、年龄、性别、地区)

### 领养流程
- 在线提交领养申请表
- 申请状态追踪
- 申请审核系统 (发布者端)

## 数据库设计 (Supabase)
### 表结构
1. users - 用户扩展信息
2. pets - 宠物信息
3. adoption_applications - 领养申请
4. favorites - 收藏记录
5. messages - 站内消息
"""

    log_file = "D:/project/wikicode/scratch/test_build_log.txt"
    with open(log_file, "w", encoding="utf-8") as f:
        f.write("=== WikiCoder Integrated Test Log ===\n")
        f.flush()

        def on_step(step):
            try:
                obs_repr = repr(step.observation)
                msg = f"\n--- Step ---\nThought: {step.thought}\nAction: {step.action_type}\nInput: {step.action_input}\nObservation (repr): {obs_repr}\n"
                print(msg, flush=True)
                f.write(msg)
                f.flush()
            except Exception as e:
                print(f"Log error: {e}")
            return True

        print(f"Starting FULL Integrated Test in: {test_dir}", flush=True)
        try:
            result = agent.run(prompt, on_step=on_step)
            f.write(f"\n[Final Report]:\n{result}\n")
            f.flush()
            
            # --- 运行态核验开始 ---
            print("\n--- Starting Runtime Verification ---", flush=True)
            f.write("\n--- Runtime Verification ---\n")
            
            # 尝试启动开发服务器 (非阻塞)
            import subprocess
            import time
            
            print("Starting dev server...", flush=True)
            # 使用 nohup 风格在后台启动，或者简单地使用 Popen
            dev_process = subprocess.Popen(["npm", "run", "dev"], cwd=test_dir, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # 等待服务器就绪
            max_retries = 30
            success = False
            for i in range(max_retries):
                try:
                    time.sleep(3)
                    with urllib.request.urlopen("http://localhost:3000", timeout=5) as response:
                        html = response.read().decode('utf-8')
                        if response.status == 200:
                            if "Build Error" in html or "use client" in html:
                                print(f"Runtime Check FAILED: Found error in page content at attempt {i+1}", flush=True)
                                f.write(f"Runtime Check FAILED: Found build error indicators in HTML.\n")
                            else:
                                print(f"Runtime Check PASSED at attempt {i+1}", flush=True)
                                f.write(f"Runtime Check PASSED: App responded with 200 OK and no build errors.\n")
                                success = True
                            break
                except Exception as e:
                    print(f"Waiting for server... ({i+1}/{max_retries}) - {e}", flush=True)
            
            if not success:
                print("Runtime Check TIMEOUT or FAILED.", flush=True)
                f.write("Runtime Check TIMEOUT or FAILED.\n")
            
            # 停止服务器
            dev_process.terminate()
            # --- 运行态核验结束 ---
            
            print("\nTest Finished.", flush=True)
        except Exception as e:
            err_msg = f"\n[Error]: {str(e)}\n"
            f.write(err_msg)
            print(f"\nTest Crashed: {e}", flush=True)

if __name__ == "__main__":
    test_pet_adoption_platform()
