"""Strategy module - Signal generation and decision agents."""

from trading_agent.strategy.candidates import (
    Candidate,
    CandidateConfig,
    CandidateGenerator,
    generate_candidates,
)
from trading_agent.strategy.decision_agent import RuleDecision, RuleDecisionAgent
from trading_agent.strategy.fast_hybrid_strategy import (
    FastDecision,
    FastHybridConfig,
    FastHybridStrategy,
)
from trading_agent.strategy.hybrid_strategy import HybridConfig, HybridDecision, HybridStrategy
from trading_agent.strategy.llm_decision_agent import LLMDecision, LLMDecisionAgent
from trading_agent.strategy.ml_decision_agent import MLDecision, MLDecisionAgent
from trading_agent.strategy.ml_features import (
    FeatureConfig,
    add_lag_features,
    add_pattern_features,
    add_return_features,
    add_session_features,
    add_technical_features,
    create_target,
    get_feature_names,
    prepare_features,
)
from trading_agent.strategy.session_features import add_intraday_patterns, get_session_stats
from trading_agent.strategy.session_features import add_session_features as add_session_features_v2
from trading_agent.strategy.shadow import (
    ShadowResult,
    ShadowTester,
    ShadowTrade,
    run_shadow_backtest,
)
from trading_agent.strategy.train_ml_model import MLTrainer, train_model
from trading_agent.strategy.train_ml_model_v2 import (
    EnhancedFeatureConfig,
    EnhancedMLTrainer,
    add_regime_features,
    create_cost_aware_target,
    train_enhanced_model,
)

__all__ = [
    # Candidates
    "Candidate",
    "CandidateConfig",
    "CandidateGenerator",
    "generate_candidates",
    # Rule Agent
    "RuleDecisionAgent",
    "RuleDecision",
    # LLM Agent
    "LLMDecisionAgent",
    "LLMDecision",
    # ML Agent
    "MLDecisionAgent",
    "MLDecision",
    # ML Features
    "FeatureConfig",
    "prepare_features",
    "create_target",
    "add_session_features",
    "add_technical_features",
    "add_pattern_features",
    "add_return_features",
    "add_lag_features",
    "get_feature_names",
    # ML Training
    "MLTrainer",
    "train_model",
    # Enhanced ML Training
    "EnhancedMLTrainer",
    "EnhancedFeatureConfig",
    "train_enhanced_model",
    "add_regime_features",
    "create_cost_aware_target",
    # Session Features
    "add_session_features_v2",
    "add_intraday_patterns",
    "get_session_stats",
    # Hybrid Strategies
    "HybridStrategy",
    "HybridConfig",
    "HybridDecision",
    "FastHybridStrategy",
    "FastHybridConfig",
    "FastDecision",
    # Shadow Testing
    "ShadowTester",
    "ShadowTrade",
    "ShadowResult",
    "run_shadow_backtest",
]
