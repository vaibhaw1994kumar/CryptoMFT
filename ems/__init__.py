"""Canonical execution layer (phase 0: framework, no behaviour change)."""
from .config import ExecutionConfig, ExecutionConfigError
from .intent import ExecutionIntent, IntentError, IntentState, Role
from .layer import ExecutionLayer

__all__ = ["ExecutionConfig", "ExecutionConfigError", "ExecutionIntent", "IntentError", "IntentState", "Role", "ExecutionLayer"]
