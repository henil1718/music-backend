import html
import json
import logging
import os
import re
import sqlite3
import time
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, unquote
from urllib.request import urlopen

load_dotenv()

logger = logging.getLogger("uvicorn.error")

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

s3_client = boto3.client(
    "s3",
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    region_name="auto",
)

app = FastAPI()

# Allow your Expo app to connect without security blockades
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "catalog.db"

# File types the catalog understands. The sync step below only picks up these extensions.
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav")

# Upload rules. Keep MAX_UPLOAD_BYTES in sync with MAX_UPLOAD_BYTES in App.js.
ALLOWED_UPLOAD_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


def get_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    with get_connection() as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(tracks)")
        }
        if columns and "artist" not in columns:
            connection.execute("DROP TABLE tracks")

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tracks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                artwork_url TEXT NOT NULL,
                url TEXT NOT NULL
            )
            """
        )


def get_bucket_storage_bytes():
    paginator = s3_client.get_paginator("list_objects_v2")
    return sum(
        int(item.get("Size") or 0)
        for page in paginator.paginate(Bucket=R2_BUCKET_NAME)
        for item in page.get("Contents", [])
    )


# --- Upload helpers ---------------------------------------------------------


def clean_name_part(value: str, fallback: str) -> str:
    """Make a title/artist safe to use inside an R2 object key."""
    value = re.sub(r"[\x00-\x1f\x7f/\\]", " ", value)  # control characters and path separators
    value = re.sub(r"_{2,}", "_", value)  # "__" is the title/artist separator, so it can't appear inside either part
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return value[:100] or fallback


def build_object_key(filename: str, extension: str) -> str:
    """
    Build a key in the "Title__Artist.ext" format that /api/sync understands.
    "My Song__Some Artist.mp3" keeps its artist; "My Song.mp3" becomes "My Song__Unknown Artist.mp3".
    """
    base_name = os.path.basename(filename.replace("\\", "/"))
    stem = os.path.splitext(base_name)[0]
    title_part, _, artist_part = stem.partition("__")
    title = clean_name_part(title_part, "Untitled")
    artist = clean_name_part(artist_part, "Unknown Artist")
    return f"{title}__{artist}{extension}"


def looks_like_audio(data: bytes, extension: str) -> bool:
    """Cheap sanity check of the file header so random files can't be stored as audio."""
    if extension == ".mp3":
        return data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)
    if extension == ".wav":
        return data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if extension == ".m4a":
        return data[4:8] == b"ftyp"
    return False


def object_exists(key: str) -> bool:
    try:
        s3_client.head_object(Bucket=R2_BUCKET_NAME, Key=key)
        return True
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


# --- Routes -----------------------------------------------------------------


@app.on_event("startup")
def startup_event():
    initialize_database()


@app.get("/health")
def health_check():
    return {"status": "online", "message": "FastAPI is running"}


@app.post("/api/sync")
def sync_tracks():
    response = s3_client.list_objects_v2(Bucket=R2_BUCKET_NAME)
    
    # 1. Get a list of all valid audio files currently in R2
    valid_keys = [
        item["Key"] for item in response.get("Contents", [])
        if item["Key"].lower().endswith(AUDIO_EXTENSIONS)
    ]

    with get_connection() as connection:
        # 2. Delete any tracks from the database that no longer exist in R2
        if valid_keys:
            placeholders = ",".join("?" for _ in valid_keys)
            connection.execute(
                f"DELETE FROM tracks WHERE id NOT IN ({placeholders})", 
                valid_keys
            )
        else:
            # If the bucket is completely empty, wipe the database
            connection.execute("DELETE FROM tracks")

        # 3. Insert new tracks as usual
        for item in response.get("Contents", []):
            key = item["Key"]
            if not key.lower().endswith(AUDIO_EXTENSIONS):
                continue

            filename = os.path.splitext(key)[0]
            parts = filename.split("__")
            if len(parts) < 2:
                title = os.path.basename(key)
                artist = "Unknown Artist"
            else:
                title = parts[0]
                artist = parts[1]

            connection.execute(
                """
                INSERT OR IGNORE INTO tracks
                    (id, title, artist, artwork_url, url)
                VALUES (?, ?, ?, ?, ?)
                """,
                (key, title, artist, "", key),
            )

    return {"synced": True}


class PresignedUrlRequest(BaseModel):
    filename: str
    content_type: str

@app.post("/api/upload/presigned")
def get_presigned_url(request: PresignedUrlRequest):
    # Decode %20 and + back into normal spaces right away
    raw_filename = request.filename or ""
    filename = unquote(raw_filename).replace("+", " ")
    
    extension = os.path.splitext(filename)[1].lower()
    content_type = request.content_type

    if extension not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Upload an MP3, WAV, or M4A file.",
        )

    key = build_object_key(filename, extension)

    try:
        if object_exists(key):
            raise HTTPException(
                status_code=409,
                detail=f"A track named '{key}' already exists. Rename the file and try again.",
            )
        
        presigned_url = s3_client.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': R2_BUCKET_NAME,
                'Key': key,
                'ContentType': content_type
            },
            ExpiresIn=3600
        )
        return {"url": presigned_url, "key": key}
    except HTTPException:
        raise
    except (ClientError, BotoCoreError):
        logger.exception("R2 presigned URL generation failed for key %r", key)
        raise HTTPException(
            status_code=502,
            detail="Could not generate upload link. Check the server logs.",
        )


@app.get("/api/tracks")
def get_tracks():
    with get_connection() as connection:
        rows = connection.execute("SELECT * FROM tracks").fetchall()

    tracks = []
    for row in rows:
        track = dict(row)
        track["url"] = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET_NAME, "Key": track["id"]},
            ExpiresIn=3600,
        )
        tracks.append(track)

    return {
        "tracks": tracks,
        "storage_used_bytes": get_bucket_storage_bytes(),
    }


@app.get("/api/tracks/{track_id}")
def get_track(track_id: str):
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT id, title, artist, artwork_url, url
            FROM tracks
            WHERE id = ?
            """,
            (track_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Track '{track_id}' not found")

    return dict(row)

@app.delete("/api/tracks/{track_id:path}")
def delete_track(track_id: str):
    clean_id = unquote(track_id)
    
    # 1. Delete the actual audio file from Cloudflare R2
    try:
        s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=clean_id)
    except (ClientError, BotoCoreError) as error:
        logger.exception("R2 deletion failed for key %r", clean_id)
        raise HTTPException(
            status_code=502, 
            detail="Failed to delete file from Cloudflare R2."
        ) from error

    # 2. Delete the track record from SQLite catalog.db
    with get_connection() as connection:
        connection.execute("DELETE FROM tracks WHERE id = ?", (clean_id,))

    return {"deleted": True, "id": clean_id}

# --- YouTube Discovery Search (Cached) --------------------------------------

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_CACHE_TTL_SECONDS = 3600  # Cache identical searches for 1 hour to save quota
MAX_YOUTUBE_CACHE_ENTRIES = 200

# { normalized_query: (timestamp, results_list) }
_youtube_search_cache = {}

ISO_DURATION_RE = re.compile(
    r"^PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def format_iso_duration(iso_str: str) -> str:
    """Convert YouTube ISO-8601 duration (e.g. 'PT3M42S') into '3:42'."""
    if not iso_str:
        return ""
    match = ISO_DURATION_RE.match(iso_str)
    if not match:
        return ""
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def fetch_youtube_json(url: str, params: dict) -> dict:
    full_url = f"{url}?{urlencode(params)}"
    try:
        with urlopen(full_url, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="ignore")
        logger.error("YouTube API HTTP %s: %s", error.code, body[:300])
        if error.code in (403, 429):
            raise HTTPException(
                status_code=503,
                detail="YouTube API quota exceeded or key restricted. Try again later.",
            ) from error
        raise HTTPException(
            status_code=502,
            detail=f"YouTube API error ({error.code}).",
        ) from error
    except (URLError, TimeoutError) as error:
        logger.error("YouTube API network error: %s", error)
        raise HTTPException(
            status_code=502,
            detail="Could not reach YouTube servers.",
        ) from error


@app.get("/api/youtube/search")
def search_youtube(q: str = Query(..., min_length=2, max_length=100)):
    if not YOUTUBE_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="YOUTUBE_API_KEY is not configured on the server.",
        )

    normalized_query = " ".join(q.strip().lower().split())
    if len(normalized_query) < 2:
        return {"results": []}

    now = time.time()
    cached = _youtube_search_cache.get(normalized_query)
    if cached and (now - cached[0]) < YOUTUBE_CACHE_TTL_SECONDS:
        return {"results": cached[1], "cached": True}

    # 1. Search YouTube videos via official Data API v3 search.list
    search_data = fetch_youtube_json(
        YOUTUBE_SEARCH_URL,
        {
            "part": "snippet",
            "q": q.strip(),
            "type": "video",
            "maxResults": 15,
            "safeSearch": "none",
            "key": YOUTUBE_API_KEY,
        },
    )

    raw_items = search_data.get("items", [])
    video_ids = [
        item.get("id", {}).get("videoId")
        for item in raw_items
        if item.get("id", {}).get("videoId")
    ]

    if not video_ids:
        return {"results": []}

    # 2. Batch-fetch video durations via videos.list (costs only 1 quota unit total)
    durations_by_id = {}
    try:
        videos_data = fetch_youtube_json(
            YOUTUBE_VIDEOS_URL,
            {
                "part": "contentDetails",
                "id": ",".join(video_ids),
                "key": YOUTUBE_API_KEY,
            },
        )
        for v_item in videos_data.get("items", []):
            vid = v_item.get("id")
            iso_dur = v_item.get("contentDetails", {}).get("duration", "")
            if vid:
                durations_by_id[vid] = format_iso_duration(iso_dur)
    except HTTPException:
        # If duration lookup fails, still return the search results without durations
        pass

    results = []
    for item in raw_items:
        video_id = item.get("id", {}).get("videoId")
        if not video_id:
            continue
        snippet = item.get("snippet", {})
        thumbnails = snippet.get("thumbnails", {})
        thumb_url = (
            thumbnails.get("high", {}).get("url")
            or thumbnails.get("medium", {}).get("url")
            or thumbnails.get("default", {}).get("url")
            or ""
        )
        results.append(
            {
                "video_id": video_id,
                "title": html.unescape(snippet.get("title") or "Untitled"),
                "channel": html.unescape(snippet.get("channelTitle") or "Unknown Channel"),
                "thumbnail_url": thumb_url,
                "duration": durations_by_id.get(video_id, ""),
            }
        )

    # Evict oldest entry if cache is full
    if len(_youtube_search_cache) >= MAX_YOUTUBE_CACHE_ENTRIES:
        oldest_key = min(_youtube_search_cache, key=lambda k: _youtube_search_cache[k][0])
        _youtube_search_cache.pop(oldest_key, None)

    _youtube_search_cache[normalized_query] = (now, results)
    return {"results": results, "cached": False}