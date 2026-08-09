"""Kitsu anime metadata provider (with ani.zip mappings for IMDb/TMDb/episode art)."""
from __future__ import annotations

import asyncio
import re
from typing import List, Optional

import httpx
from rapidfuzz import fuzz

from Backend.helper.metadata.common import (
    ensure_media_ids,
    KITSU_CACHE,
    KITSU_THRESHOLD,
    cached_call,
    logo_from_imdb,
    normalize_rating,
    parse_year_range,
    strip_html,
)
from Backend.logger import LOGGER

KITSU_URL = "https://kitsu.io/api/edge"
ANIZIP_URL = "https://api.ani.zip/mappings"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

_HEADERS = {
    "Accept": "application/vnd.api+json",
    "Content-Type": "application/vnd.api+json",
    "User-Agent": "Telegram-Stremio (+https://github.com/weebzone/Telegram-Stremio)",
}


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=_HEADERS)
        return _client


def _normalize(title: str) -> str:
    if not title:
        return ""
    t = title.lower().strip()
    t = re.sub(r"^\b(the|a|an)\b\s+", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _fuzzy(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    try:
        return max(fuzz.token_set_ratio(a, b), fuzz.token_sort_ratio(a, b)) / 100.0
    except Exception:
        return 0.0


def _title_score(query: str, attrs: dict) -> float:
    """Score against every Kitsu title variant + abbreviations (alias-aware)."""
    from Backend.helper.metadata.common import score_candidate_aliases
    titles = attrs.get("titles") or {}
    primary = (
        attrs.get("canonicalTitle")
        or titles.get("en")
        or titles.get("en_jp")
        or titles.get("ja_jp")
        or ""
    )
    aliases = []
    aliases.extend(titles.values() if isinstance(titles, dict) else [])
    aliases.extend(attrs.get("abbreviatedTitles") or [])
    # slug as last-resort alias (e.g. one-piece -> one piece)
    slug = attrs.get("slug")
    if slug:
        aliases.append(str(slug).replace("-", " "))
    return score_candidate_aliases(
        query, None, primary, 0,
        aliases=aliases,
        year_reliable=False,
        year_lower_bound=True,
    )


def _season_queries(title: str, season: Optional[int]) -> List[str]:
    if season and int(season) > 1:
        return [f"{title} Season {season}", f"{title} {season}", title]
    return [title]


async def _kitsu_search(query: str, subtype: Optional[str] = None) -> Optional[dict]:
    try:
        client = await _get_client()
        params = {"filter[text]": query, "page[limit]": 8}
        if subtype:
            params["filter[subtype]"] = subtype
        resp = await client.get(f"{KITSU_URL}/anime", params=params)
        if resp.status_code != 200:
            return None
        data = (resp.json() or {}).get("data") or []
        return data
    except Exception as e:
        LOGGER.warning(f"[KITSU] search failed for '{query}': {e}")
        return None


async def search_anime(title: str, season: Optional[int] = None, movie: bool = False) -> Optional[dict]:
    cache_key = f"kitsu::{'movie' if movie else 'tv'}::{title}::{season}"

    async def _produce():
        best = None
        best_score = 0.0
        subtype = "movie" if movie else None
        for query in _season_queries(title, None if movie else season):
            rows = await _kitsu_search(query, subtype=subtype) or []
            for row in rows:
                attrs = row.get("attributes") or {}
                # Prefer TV for series searches
                if not movie and attrs.get("subtype") in ("movie", "music"):
                    continue
                if movie and attrs.get("subtype") not in ("movie", None, "special", "OVA", "ONA"):
                    # still allow movie subtype primarily
                    if attrs.get("subtype") not in ("movie",):
                        continue
                score = _title_score(title, attrs)
                if score > best_score:
                    best_score = score
                    best = row
            if best_score >= 0.92:
                break

        if best and best_score >= KITSU_THRESHOLD:
            attrs = best.get("attributes") or {}
            LOGGER.info(
                f"[KITSU] match '{title}' -> '{attrs.get('canonicalTitle')}' "
                f"[{best.get('id')}] score={best_score:.2f}"
            )
            return best
        if best:
            attrs = best.get("attributes") or {}
            LOGGER.info(
                f"[KITSU] low-confidence for '{title}': "
                f"'{attrs.get('canonicalTitle')}' score={best_score:.2f}"
            )
        return None

    return await cached_call(KITSU_CACHE, cache_key, "kitsu_search", _produce)


async def get_anizip_mappings(kitsu_id: int) -> Optional[dict]:
    cache_key = f"anizip::{kitsu_id}"

    async def _produce():
        try:
            client = await _get_client()
            resp = await client.get(ANIZIP_URL, params={"kitsu_id": kitsu_id})
            return resp.json() if resp.status_code == 200 else None
        except Exception as e:
            LOGGER.warning(f"[KITSU] ani.zip mappings failed for {kitsu_id}: {e}")
            return None

    return await cached_call(KITSU_CACHE, cache_key, "anizip", _produce)


def _anizip_image(images, cover_type: str) -> str:
    for img in images or []:
        if str(img.get("coverType", "")).lower() == cover_type.lower() and img.get("url"):
            return img["url"]
    return ""


def _poster(attrs: dict, images: list) -> str:
    poster = attrs.get("posterImage") or {}
    return (
        poster.get("original")
        or poster.get("large")
        or poster.get("medium")
        or _anizip_image(images, "Poster")
        or ""
    )


def _backdrop(attrs: dict, images: list) -> str:
    cover = attrs.get("coverImage") or {}
    return (
        cover.get("original")
        or cover.get("large")
        or _anizip_image(images, "Fanart")
        or _anizip_image(images, "Banner")
        or ""
    )


def _common_payload(row: dict, doc: dict, title: str) -> dict:
    attrs = row.get("attributes") or {}
    mappings = (doc or {}).get("mappings") or {}
    tmdb_id = mappings.get("themoviedb_id")
    try:
        tmdb_id = int(tmdb_id) if tmdb_id else None
    except (ValueError, TypeError):
        tmdb_id = None

    titles = attrs.get("titles") or {}
    images = (doc or {}).get("images") or []
    rate = normalize_rating(
        (float(attrs["averageRating"]) / 10.0) if attrs.get("averageRating") else 0
    )
    year, year_end = parse_year_range(attrs.get("startDate"), attrs.get("endDate"))
    duration = attrs.get("episodeLength")
    imdb_id = mappings.get("imdb_id")
    english = titles.get("en") or titles.get("en_us") or titles.get("en_jp") or ""
    original = (
        attrs.get("canonicalTitle")
        or titles.get("ja_jp")
        or titles.get("en_jp")
        or title
    )
    display = english or original or title
    logo = _anizip_image(images, "Clearlogo") or logo_from_imdb(imdb_id)
    payload = {
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "title": display,
        "title_english": english or display,
        "original_title": original if original != display else "",
        "year": year,
        "year_end": year_end,
        "rate": rate,
        "description": strip_html(attrs.get("synopsis") or attrs.get("description") or ""),
        "poster": _poster(attrs, images),
        "backdrop": _backdrop(attrs, images),
        "logo": logo,
        "genres": [],
        "cast": [],
        "runtime": f"{duration} min" if duration else "",
        "kitsu_id": row.get("id"),
    }
    return ensure_media_ids(payload, seed=f"kitsu:{row.get('id')}")


def _episode_title(ep: dict, season: int, episode: int, absolute: bool = False) -> str:
    ep_title = None
    if isinstance(ep.get("title"), dict):
        ep_title = ep["title"].get("en") or ep["title"].get("ja")
    elif isinstance(ep.get("title"), str):
        ep_title = ep.get("title")
    if ep_title:
        return ep_title
    if absolute or season is None:
        return f"Episode {episode}"
    return f"S{int(season):02d}E{int(episode):02d}"


def _find_anizip_episode(episodes: dict, season, episode, absolute: bool) -> dict:
    """Locate the ani.zip episode entry for an absolute or S/E number."""
    if not episodes:
        return {}
    ep_num = int(episode)

    # 1) Direct key match (most ani.zip catalogs key by absolute / sequential number)
    if str(ep_num) in episodes:
        return episodes[str(ep_num)] or {}

    # 2) Match absoluteEpisodeNumber (TVDB absolute)
    for candidate in episodes.values():
        try:
            if int(candidate.get("absoluteEpisodeNumber") or -1) == ep_num:
                return candidate
        except (TypeError, ValueError):
            continue

    # 3) Match episode / episodeNumber fields
    for candidate in episodes.values():
        try:
            if int(candidate.get("episodeNumber") or candidate.get("episode") or -1) == ep_num:
                if absolute or season is None:
                    return candidate
                if int(candidate.get("seasonNumber") or candidate.get("season") or -1) == int(season):
                    return candidate
        except (TypeError, ValueError):
            continue

    # 4) Season + relative episode when both known
    if season is not None and not absolute:
        for candidate in episodes.values():
            try:
                if (
                    int(candidate.get("seasonNumber") or -1) == int(season)
                    and int(candidate.get("episodeNumber") or candidate.get("episode") or -1) == ep_num
                ):
                    return candidate
            except (TypeError, ValueError):
                continue
    return {}


def _resolve_episode_slot(doc: dict, season, episode, absolute: bool) -> tuple:
    """Map (season, episode) or absolute episode to IMDb-style S/E via ani.zip.

    ani.zip entries often carry:
      - seasonNumber / episodeNumber  → aired-order IMDb/TVDB style slot
      - absoluteEpisodeNumber         → continuous absolute number
    Prefer the mapped seasonNumber+episodeNumber so Stremio arranges episodes
    correctly even when the filename used absolute numbering.
    """
    episodes = (doc or {}).get("episodes") or {}
    ep = _find_anizip_episode(episodes, season, episode, absolute)

    mapped_season = ep.get("seasonNumber") if ep else None
    mapped_ep = ep.get("episodeNumber") if ep else None
    if mapped_ep is None and ep:
        mapped_ep = ep.get("episode")

    try:
        mapped_season = int(mapped_season) if mapped_season is not None else None
    except (TypeError, ValueError):
        mapped_season = None
    try:
        mapped_ep = int(mapped_ep) if mapped_ep is not None else None
    except (TypeError, ValueError):
        mapped_ep = None

    if absolute or season is None:
        # Prefer provider-mapped S/E; fall back to S1 + absolute
        use_season = mapped_season if mapped_season is not None else 1
        use_episode = mapped_ep if mapped_ep is not None else int(episode)
        return use_season, use_episode, ep, True

    use_season = mapped_season if mapped_season is not None else int(season)
    use_episode = mapped_ep if mapped_ep is not None else int(episode)
    return use_season, use_episode, ep, False



async def resolve_absolute_episode(
    title: str,
    season: int,
    episode: int,
    year=None,
) -> Optional[int]:
    if not title or season is None or episode is None:
        return None
    try:
        season_i, episode_i = int(season), int(episode)
    except (TypeError, ValueError):
        return None

    row = await search_anime(title, season=season_i, movie=False)
    if not row:
        row = await search_anime(title, season=None, movie=False)
    if not row:
        return None

    try:
        kitsu_id = int(row["id"])
    except (TypeError, ValueError, KeyError):
        return None

    doc = await get_anizip_mappings(kitsu_id) or {}
    episodes = doc.get("episodes") or {}
    if not episodes:
        return None

    ep = _find_anizip_episode(episodes, season_i, episode_i, absolute=False)
    if not ep:
        return None

    abs_val = ep.get("absoluteEpisodeNumber")
    if abs_val in (None, ""):
        for key, candidate in episodes.items():
            try:
                if (
                    int(candidate.get("seasonNumber") or -1) == season_i
                    and int(candidate.get("episodeNumber") or candidate.get("episode") or -1) == episode_i
                ):
                    abs_val = candidate.get("absoluteEpisodeNumber") or key
                    break
            except (TypeError, ValueError):
                continue
    try:
        abs_i = int(abs_val)
        if abs_i > 0:
            return abs_i
    except (TypeError, ValueError):
        pass
    return None


async def fetch_anime_tv(
    title,
    season,
    episode,
    encoded_string,
    year=None,
    quality=None,
    absolute: bool = False,
) -> Optional[dict]:
    # For absolute episodes, search without season suffix (One Piece not "One Piece Season 21")
    search_season = None if absolute or season is None else season
    row = await search_anime(title, season=search_season, movie=False)
    if not row:
        LOGGER.info(f"[KITSU] No match for '{title}' (season={season}, absolute={absolute})")
        return None

    try:
        kitsu_id = int(row["id"])
    except (TypeError, ValueError, KeyError):
        return None

    doc = await get_anizip_mappings(kitsu_id) or {}
    payload = _common_payload(row, doc, title)

    season_number, episode_number, ep, is_abs = _resolve_episode_slot(
        doc, season, episode, absolute or season is None
    )

    if is_abs and not ep:
        LOGGER.info(
            f"[KITSU] Absolute episode {episode} not in ani.zip for '{title}' "
            f"(kitsu={kitsu_id}) — still indexing with season={season_number}"
        )

    payload.update({
        "media_type": "tv",
        "season_number": season_number,
        "episode_number": episode_number,
        "episode_title": _episode_title(ep, season_number, episode_number, absolute=is_abs),
        "episode_backdrop": ep.get("image", "") or "",
        "episode_overview": ep.get("overview") or ep.get("summary") or "",
        "episode_released": ep.get("airDate") or ep.get("airdate") or "",
        "quality": quality,
        "encoded_string": encoded_string,
        "absolute_episode": (
            int(episode) if is_abs else (
                int(ep["absoluteEpisodeNumber"])
                if ep and ep.get("absoluteEpisodeNumber") not in (None, "")
                else None
            )
        ),
    })
    return payload


async def fetch_anime_movie(title, encoded_string, year=None, quality=None) -> Optional[dict]:
    row = await search_anime(title, movie=True)
    if not row:
        # also try without subtype filter
        row = await search_anime(title, movie=False)
        if row:
            subtype = ((row.get("attributes") or {}).get("subtype") or "").lower()
            if subtype not in ("movie", "special", "ova", "ona", ""):
                LOGGER.info(f"[KITSU] No movie match for '{title}'")
                return None
    if not row:
        LOGGER.info(f"[KITSU] No movie match for '{title}'")
        return None

    try:
        kitsu_id = int(row["id"])
    except (TypeError, ValueError, KeyError):
        return None

    doc = await get_anizip_mappings(kitsu_id) or {}
    payload = _common_payload(row, doc, title)
    payload.update({"media_type": "movie", "quality": quality, "encoded_string": encoded_string})
    return payload
