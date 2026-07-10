"""Report generation. Every number carries the query that produced it."""

from campaign_copilot.reporting.builder import ChannelRow, WeeklyReview, build_weekly_review
from campaign_copilot.reporting.figures import Figure, FigureSet, ProvenanceError
from campaign_copilot.reporting.render import render_markdown, render_pptx

__all__ = [
    "ChannelRow",
    "Figure",
    "FigureSet",
    "ProvenanceError",
    "WeeklyReview",
    "build_weekly_review",
    "render_markdown",
    "render_pptx",
]
