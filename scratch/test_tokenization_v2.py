
from src.core.query_rewriter import _tokenize, load_business_terms
from src.utils.config import load_config
from pathlib import Path

def test_enhanced_tokenization():
    query = "2026年家客装移机工时费采购结算单已经结算了多少费用？"
    
    # 模拟从配置加载业务词
    workspace_path = Path("d:/project/wikicode/data/dictionaries/business_terms.yaml")
    core_keywords = load_business_terms(workspace_path)
    print(f"Loaded Core Keywords: {core_keywords}")

    tokens = _tokenize(query, core_keywords=core_keywords)
    print(f"\nQuery: {query}")
    print(f"Tokens ({len(tokens)}): {tokens}")

    # 检查核心词是否在 tokens 中
    core_words = ["结算单", "费用", "金额", "2026", "采购", "结算"]
    for word in core_words:
        found = any(word.lower() in t.lower() for t in tokens)
        print(f"Check '{word}': {'FOUND' if found else 'MISSING'}")

if __name__ == "__main__":
    test_enhanced_tokenization()
