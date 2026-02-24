"""Template matching module."""

from libs.shared.template.matcher import TemplateMatcher, MatchResult
from libs.shared.template.phash import PerceptualHasher
from libs.shared.template.orb_matcher import OrbMatcher

__all__ = ["TemplateMatcher", "MatchResult", "PerceptualHasher", "OrbMatcher"]
