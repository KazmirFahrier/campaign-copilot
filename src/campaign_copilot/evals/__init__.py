"""The evaluation harness. The point of the project."""

from campaign_copilot.evals.dataset import (
    AdversarialCase,
    GoldenCase,
    MultiTurnCase,
    load_adversarial,
    load_golden,
    load_multi_turn,
)
from campaign_copilot.evals.metrics import cohens_kappa, multiset_f1, result_set_match
from campaign_copilot.evals.policies import CompliantPolicy, NaivePolicy, OraclePolicy
from campaign_copilot.evals.report import check_regression, render_report, save_history
from campaign_copilot.evals.runner import Ablation, EvalRunner, Report

__all__ = [
    "Ablation",
    "AdversarialCase",
    "CompliantPolicy",
    "EvalRunner",
    "GoldenCase",
    "MultiTurnCase",
    "NaivePolicy",
    "OraclePolicy",
    "Report",
    "check_regression",
    "cohens_kappa",
    "load_adversarial",
    "load_golden",
    "load_multi_turn",
    "multiset_f1",
    "render_report",
    "result_set_match",
    "save_history",
]
