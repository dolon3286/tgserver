"""Filename / caption parsing helpers."""
from __future__ import annotations

import re
import traceback

import PTN
from guessit import guessit as _guessit

from Backend.helper.metadata.common import COMBINED_EPISODE_BASE, COMBINED_SEASON, first
from Backend.helper.split_files import parse_combined_episodes, parse_split_info, strip_part_suffix
from Backend.logger import LOGGER

_MULTIPART_RE = re.compile(r"(?:part|cd|disc|disk)[s._-]*\d+(?=\.\w+$)", re.IGNORECASE)


def parse_media_name(name: str) -> dict:
    try:
        ptn = PTN.parse(name) or {}
    except Exception as e:
        LOGGER.warning(f"PTN parsing failed for {name}: {e}")
        ptn = {}

    parsed = {
        "title": ptn.get("title"),
        "year": ptn.get("year"),
        "season": ptn.get("season"),
        "episode": ptn.get("episode"),
        "quality": ptn.get("resolution"),
        "excess": ptn.get("excess"),
    }

    if _guessit:
        try:
            g = _guessit(name)
            parsed["title"] = parsed["title"] or first(g.get("title"))
            parsed["year"] = parsed["year"] or first(g.get("year"))
            parsed["season"] = parsed["season"] or first(g.get("season"))
            parsed["episode"] = parsed["episode"] or first(g.get("episode"))
            parsed["quality"] = parsed["quality"] or first(g.get("screen_size"))
        except Exception as e:
            LOGGER.warning(f"GuessIt parsing failed for {name}: {e}")

    return parsed


def apply_combined_override(payload: dict, combined: dict) -> None:
    season, start, end = combined["season"], combined["start"], combined["end"]
    payload["season_number"] = COMBINED_SEASON
    payload["episode_number"] = COMBINED_EPISODE_BASE + season
    payload["episode_title"] = f"Season {season} Combined"
    label = "Full" if start is None else f"E{start:02d}-E{end:02d}"
    payload["quality"] = f"{payload.get('quality') or 'HD'} {label}"
    if not payload.get("episode_backdrop"):
        payload["episode_backdrop"] = payload.get("backdrop") or payload.get("poster") or ""


def is_multipart_video(filename: str) -> bool:
    return bool(_MULTIPART_RE.search(filename or ""))



# Absolute / orphan episode patterns (no SxxExx), e.g. "One Piece 1223 720.mkv"
_SEASON_EP_RE = re.compile(r"(?i)s\d{1,2}[._\s-]*e\d{1,3}")
# Quality / codec tokens (with or without trailing 'p')
_QUALITY_TOKEN_RE = re.compile(
    r"(?i)(?:(?<![\w])(?:240|360|480|576|720|1080|1440|2160|4320)p?(?![\w])|"
    r"\d{3,4}x\d{3,4}|web-?dl|blu-?ray|hdtv|hdrip|webrip|x264|x265|hevc|avc|aac|dts|truehd|atmos|10bit|8bit)"
)
_YEAR_RE = re.compile(r"(?:^|[\s._\-(])((?:19|20)\d{2})(?:[\s._\-)]|$)")


def extract_absolute_episode(filename: str, parsed: dict | None = None) -> int | None:
    """Return absolute episode number when no season is present.

    Handles styles like:
      One Piece 1223 720.mkv
      One Piece - 1223 720p.mkv
      Naruto 500 1080p.mkv
    """
    parsed = parsed or {}
    if parsed.get("season") is not None:
        return None
    if _SEASON_EP_RE.search(filename or ""):
        return None

    ep = parsed.get("episode")
    if isinstance(ep, list):
        return None
    if ep is not None:
        try:
            return int(ep)
        except (TypeError, ValueError):
            pass

    name = filename or ""
    # Strip extension and quality/codec tokens
    cleaned = re.sub(r"\.[a-z0-9]{2,4}$", " ", name, flags=re.I)
    cleaned = _QUALITY_TOKEN_RE.sub(" ", cleaned)
    # Strip years so 2021 is not treated as an episode
    cleaned = _YEAR_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[\s._-]+", " ", cleaned).strip()

    # Explicit E/EP/Episode prefix wins
    prefixed = re.findall(r"(?i)(?:^|\s)(?:e|ep|episode)\s*0*(\d{1,4})(?:\s|$)", cleaned)
    if prefixed:
        return int(prefixed[-1])

    # Bare numbers left after stripping quality/year — prefer last 2–4 digit token
    bare = re.findall(r"(?:^|\s)(\d{2,4})(?:\s|$)", cleaned)
    if not bare:
        return None
    # Prefer 3–4 digit (typical anime absolute); fall back to last remaining
    long = [int(x) for x in bare if len(x) >= 3]
    if long:
        return long[-1]
    return int(bare[-1])


def is_absolute_episode(parsed: dict, filename: str = "") -> bool:
    """True when we have an episode number but no season (orphan/absolute style)."""
    if parsed.get("season") is not None:
        return False
    if _SEASON_EP_RE.search(filename or ""):
        return False
    if parsed.get("episode") is not None and not isinstance(parsed.get("episode"), list):
        return True
    return extract_absolute_episode(filename, parsed) is not None

def analyze_metadata_failure(filename: str) -> str:
    if is_multipart_video(filename or ""):
        return (
            "Looks like a multi-part video split (e.g. part1 / cd1) that can't be "
            "combined for streaming."
        )

    split_info = parse_split_info(filename or "")
    parse_target = strip_part_suffix(filename) if split_info else (filename or "")

    try:
        parsed = parse_media_name(parse_target)
    except Exception:
        return (
            "The file name / caption could not be parsed. Give it a clear name like "
            "'Movie Name (2021) 1080p'."
        )

    combined = parse_combined_episodes(parse_target)
    excess = parsed.get("excess")
    if not combined and excess and any("combined" in str(item).lower() for item in excess):
        return (
            "The caption says 'combined' but no season number could be read from it "
            "(e.g. name it 'Show S02 Combined')."
        )

    title = parsed.get("title")
    season = parsed.get("season")
    episode = parsed.get("episode")
    quality = parsed.get("quality")

    if not combined and (isinstance(season, list) or isinstance(episode, list)):
        return (
            "The name spans multiple seasons (e.g. S01-S03) that can't be filed as one entry. "
            "Upload one season per file. Combined episode packs within a single season are fine "
            "when named like 'Show S02 E01-E05' or 'Show S02 Combined'."
        )
    if not quality:
        return (
            "No video quality/resolution was found. Add one to the caption "
            "(e.g. 480p, 720p, 1080p or 2160p)."
        )
    if not title:
        return "No title could be detected. Rename or caption the file with a clear title."

    return (
        "Could not match this title on the configured providers. Fix the title/year in the "
        "caption, or add an IMDb link/id (tt...) or a TMDB link/id, then forward it again."
    )
