import json
import os
from pathlib import Path
import psycopg2
import yaml

def load_db_config():
    # 模拟从 WikiCoder 配置中加载数据库连接
    config_path = Path(".wikicoder/config.yaml")
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
            return cfg.get("database", {})
    return {}

def get_pure_business_graph():
    """获取源文档纯净版图谱数据"""
    path = Path("graphify_out/.graphify_pure_merged.json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"nodes": [], "edges": []}

def run_business_audit():
    """
    执行基于图谱逻辑的业务审计
    返回结构化异常报告
    """
    db_cfg = load_db_config()
    if not db_cfg:
        return {"status": "error", "message": "未配置数据库连接"}

    try:
        # 建立连接
        conn = psycopg2.connect(**db_cfg)
        cur = conn.cursor()
        
        # 审计项 1: 超期未安装终端 (基于图谱中的 48 小时逻辑)
        # 这里仅作演示，实际应通过图谱动态生成 SQL 或调用之前验证过的脚本
        cur.execute("""
            SELECT sn, use_date - create_date as diff 
            FROM terminal_con 
            WHERE status = '未安装' AND (use_date - create_date) > INTERVAL '48 hours'
            LIMIT 100
        """)
        rows = cur.fetchall()
        
        report = {
            "timestamp": os.path.getmtime(".wikicoder/config.yaml"),
            "anomalies": [
                {"type": "安装超期", "sn": r[0], "details": f"已积压 {r[1]}"} for r in rows
            ],
            "summary": f"共发现 {len(rows)} 项安装时效违规项"
        }
        
        cur.close()
        conn.close()
        return report
    except Exception as e:
        return {"status": "error", "message": str(e)}
