"""Canonical execution layer (phase 0: framework, no behaviour change).
Temporary re-export (Phase A, removed in Phase F): the modules moved to crypto_stat_arb.ems; observation, flicker_fit and offline_hedge stay here (research and record-only tools)."""
from crypto_stat_arb.ems.config import ExecutionConfig, ExecutionConfigError
from crypto_stat_arb.ems.intent import ExecutionIntent, IntentError, IntentState, Role
from crypto_stat_arb.ems.layer import ExecutionLayer

__all__ = ["ExecutionConfig", "ExecutionConfigError", "ExecutionIntent", "IntentError", "IntentState", "Role", "ExecutionLayer"]
