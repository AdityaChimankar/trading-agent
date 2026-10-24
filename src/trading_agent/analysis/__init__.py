"""Analysis module - Opportunity finding, rankings, technical analysis."""

from trading_agent.analysis.digest import (
    DailyDigest,
    DigestGenerator,
    generate_morning_briefing,
)
from trading_agent.analysis.opportunity_finder import (
    DEFAULT_OPPORTUNITY_CONFIG,
    Opportunity,
    OpportunityConfig,
    OpportunityFinder,
)
from trading_agent.analysis.rankings import (
    RankedSymbol,
    combine_rankings,
    composite_score,
    percentile_rank,
    rank_by_mean_reversion,
    rank_by_momentum,
    rank_by_volume_surge,
    z_score_rank,
)
from trading_agent.analysis.sentiment import (
    SentimentAnalyzer,
    SentimentLabel,
    SentimentScore,
    get_sentiment_analyzer,
)
from trading_agent.analysis.technical_summary import (
    TechnicalAnalyzer,
    TechnicalSummary,
    generate_summary_text,
    get_technical_summary,
    technical_votes,
)

__all__ = [
    # Opportunity Finder
    "Opportunity",
    "OpportunityConfig",
    "OpportunityFinder",
    "DEFAULT_OPPORTUNITY_CONFIG",
    # Rankings
    "RankedSymbol",
    "rank_by_momentum",
    "rank_by_mean_reversion",
    "rank_by_volume_surge",
    "combine_rankings",
    "percentile_rank",
    "z_score_rank",
    "composite_score",
    # Technical Summary
    "TechnicalSummary",
    "TechnicalAnalyzer",
    "generate_summary_text",
    "get_technical_summary",
    "technical_votes",
    # Sentiment
    "SentimentLabel",
    "SentimentScore",
    "SentimentAnalyzer",
    "get_sentiment_analyzer",
    # Digest
    "DailyDigest",
    "DigestGenerator",
    "generate_morning_briefing",
]
