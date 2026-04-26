import os
import sys
import time
import json
from pathlib import Path

# 将项目源码路径加入系统路径
sys.path.append(str(Path(__file__).parent.parent))

from src.core.build_agent import BuildAgent
from src.utils.config import AppConfig

def run_advanced_test_suite():
    # 1. 加载配置
    # 尝试从当前目录或父目录寻找配置
    potential_paths = [
        Path(".wikicoder/config.yaml"),
        Path("../.wikicoder/config.yaml"),
        Path("D:/project/wikicode/.wikicoder/config.yaml")
    ]
    config_path = None
    for p in potential_paths:
        if p.exists():
            config_path = p
            break
            
    if not config_path:
        print(f"FAILED: 找不到配置文件。请确保在项目根目录运行。")
        return
    
    from src.utils.config import load_config
    print(f"INFO: 使用配置文件: {config_path.absolute()}")
    config = load_config(config_path)
    agent = BuildAgent(config)
    
    # 2. 定义高阶测试用例
    test_cases = [
        {
            "id": "RAG-ADV-01",
            "name": "政策冲突与时效性判定",
            "query": "根据《家庭宽带业务终端管理办法》和《网络纪要〔2026〕11号会议纪要》，如果一个老旧终端型号不在废旧处置目录中，但已经使用了超过5年且雷击损坏，我应该按什么流程处理？请给出最符合最新政策的方案。",
            "category": "RAG Reasoning"
        },
        {
            "id": "BUILD-ADV-01",
            "name": "数据挖掘与可视化报告",
            "query": "读取 D:\\代维\\test 下的所有 Excel。按‘分公司’汇总‘企宽’和‘家宽’的开通总量。并给出各分公司的统计列表及简短分析。",
            "category": "E2E Automation"
        }
    ]
    
    print(f"START: 开始执行 WikiCoder 高阶智能测试集 (共 {len(test_cases)} 项)...")
    results = []

    for case in test_cases:
        print(f"\n" + "="*60)
        print(f"CASE: [{case['id']}] {case['name']}")
        print(f"TYPE: {case['category']}")
        
        start_time = time.time()
        
        # 定义回调以便实时观察进度
        def on_step_callback(step):
            print(f"\n[Agent Thought]: {step.thought}")
            print(f"[Action {step.action_type}]: {step.action_input}")
            return True 

        try:
            # 运行 Agent
            final_report = agent.run(case["query"], on_step=on_step_callback)
            duration = time.time() - start_time
            
            results.append({
                "id": case["id"],
                "name": case["name"],
                "duration": f"{duration:.2f}s",
                "report": final_report,
                "steps_count": len(agent.steps),
                "status": "PASS" if "报错" not in final_report else "FAIL"
            })
        except Exception as e:
            print(f"ERROR: 执行出错: {str(e)}")
            results.append({
                "id": case["id"],
                "name": case["name"],
                "duration": "0s",
                "report": f"执行崩溃: {str(e)}",
                "steps_count": 0,
                "status": "ERROR"
            })
        
        # 清除步骤历史，准备下一个用例
        agent.steps = []

    # 3. 输出汇总报告
    print(f"\n" + "X"*60)
    print("FINISH: 测试任务全部完成！总结报告如下：")
    for res in results:
        indicator = "[OK]" if res["status"] == "PASS" else "[FAIL]"
        print(f"{indicator} {res['id']} | {res['status']} | 步数: {res['steps_count']} | 耗时: {res['duration']}")
    
    # 将结果保存到文件
    report_file = Path("tests/latest_test_report.json")
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nREPORT: 详细测试报告已保存至: {report_file.absolute()}")

if __name__ == "__main__":
    run_advanced_test_suite()
