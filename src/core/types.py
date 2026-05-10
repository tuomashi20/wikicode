from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, List, Optional, Dict

@dataclass
class ExecutionResult:
    success: bool
    observation: str
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class BuildStep:
    thought: str
    action_type: str
    action_input: str
    observation: str = ""
    status: str = "success"
    raw_data: Optional[Any] = None # [NEW] 存储原始结构化结果
    tasks: List[str] = field(default_factory=list)
    self_criticism: str = ""
