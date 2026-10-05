import io
import re
import zipfile
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from Backend import db
from Backend.helper.settings_manager import SettingsManager

router = APIRouter(prefix="/emby", tags=["Emby Integration"])

def sanitize_filename(name: str) -> str:
    if not name:
        return "Unknown"
    # Replace illegal characters with spaces or safe alternatives
    # Illegal on Windows: < > : " / \ | ? *
    name = re.sub(r'[\/\\:]', ' - ', name)
    name = re.sub(r'[<>"\|\?\*]', '', name)
    return name.strip()

@router.get("/{token}/library.zip")
async def get_emby_library_zip(request: Request, token: str):
    token_doc = await db.get_api_token(token)
    if not token_doc:
        raise HTTPException(status_code=403, detail="Invalid token")

    base_url = SettingsManager.current().base_url or str(request.base_url).rstrip("/")
    if base_url.endswith("/"):
        base_url = base_url[:-1]

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        # We need to iterate over all storage databases.
        # Check all available db keys in db.dbs
        for db_key in db.dbs.keys():
            if not db_key.startswith("storage"):
                continue

            storage_db = db.dbs[db_key]

            # Get Movies
            movies_col = storage_db["movie"]
            async for movie in movies_col.find({}):
                if "telegram" not in movie or not movie["telegram"]:
                    continue
                title = sanitize_filename(movie.get("title", "Unknown"))
                year = movie.get("year", movie.get("release_year", ""))

                quality = movie["telegram"][0]
                stream_id = quality.get("id")
                if not stream_id:
                    continue

                folder_name = f"{title} ({year})" if year else title
                filename = f"Movies/{folder_name}/{folder_name}.strm"

                stream_url = f"{base_url}/dl/{token}/{stream_id}/video.mkv"

                zip_file.writestr(filename, stream_url)

            # Get TV Shows
            shows_col = storage_db["tv"]
            async for show in shows_col.find({}):
                title = sanitize_filename(show.get("title", "Unknown"))
                year = show.get("year", show.get("release_year", ""))

                show_folder_name = f"{title} ({year})" if year else title

                seasons = show.get("seasons", [])
                for season in seasons:
                    season_num = season.get("season_number", 1)
                    for episode in season.get("episodes", []):
                        ep_num = episode.get("episode_number", 1)
                        if "telegram" not in episode or not episode["telegram"]:
                            continue

                        quality = episode["telegram"][0]
                        stream_id = quality.get("id")
                        if not stream_id:
                            continue

                        season_str = str(season_num).zfill(2)
                        ep_str = str(ep_num).zfill(2)
                        filename = f"Shows/{show_folder_name}/Season {season_str}/{title} S{season_str}E{ep_str}.strm"

                        stream_url = f"{base_url}/dl/{token}/{stream_id}/video.mkv"

                        zip_file.writestr(filename, stream_url)

    zip_buffer.seek(0)

    return Response(
        content=zip_buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=emby_library_{token}.zip"}
    )
