import logging
import os
import re
import sqlite3
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from urllib.parse import unquote

load_dotenv()

logger = logging.getLogger("uvicorn.error")

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")

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