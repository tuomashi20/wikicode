import sys
import os
from pathlib import Path

# 添加项目根目录到路径
sys.path.append(os.getcwd())

from src.utils.config import load_config
from src.core.build_agent import BuildAgent

def main():
    config = load_config()
    agent = BuildAgent(config)
    
    query = "OLT设备是哪个专业应该维护的呢？"
    print(f"\n[START TEST]: {query}")
    print("-" * 50)
    
    def on_step(step):
        print(f"\n[THOUGHT]: {step.thought}")
        print(f"[ACTION]: {step.action_type}({step.action_input})")
        # print(f"[OBSERVATION]: {step.observation[:200]}...") # 暂时隐藏详细观察，保持简洁
        return True

    final_answer = agent.run(query, on_step=on_step)
    
    print("\n" + "=" * 50)
    print("[FINAL ANSWER]:")
    print(final_answer)
    print("=" * 50)

if __name__ == "__main__":
    main()
