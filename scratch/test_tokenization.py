
from src.core.query_rewriter import _tokenize
import re

def test_user_query():
    query = "2026年家客装移机工时费采购结算单已经结算了多少费用？"
    tokens = _tokenize(query)
    print(f"Query: {query}")
    print(f"Tokens ({len(tokens)}): {tokens}")

    # 检查核心词是否在 tokens 中
    core_words = ["结算单", "费用", "金额", "2026", "采购"]
    for word in core_words:
        found = any(word in t or t in word for t in tokens)
        print(f"Check '{word}': {'FOUND' if found else 'MISSING'}")

if __name__ == "__main__":
    test_user_query()
