"""Smart Music Companion MVP core package."""

from .audio_generator import AudioGenerationError, generate_music_wav
from .gait import build_walking_guidance
from .recommender import RecommendationError, UserContext, build_profile, recommend
from .calendar_model import CalendarEvent, find_conflicts, validate_events
from .ics_parser import parse_ics
from .planner import BreakPlanItem, music_context_for_plan, plan_breaks
from .state import StatusSnapshot, parse_status_import

__all__ = [
    "AudioGenerationError",
    "RecommendationError",
    "UserContext",
    "build_profile",
    "generate_music_wav",
    "build_walking_guidance",
    "recommend",
    "CalendarEvent",
    "find_conflicts",
    "validate_events",
    "parse_ics",
    "BreakPlanItem",
    "plan_breaks",
    "music_context_for_plan",
    "StatusSnapshot",
    "parse_status_import",
]
