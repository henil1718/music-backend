import os
import sqlite3

import boto3
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

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


@app.on_event("startup")
def startup_event():
    initialize_database()


@app.get("/health")
def health_check():
    return {"status": "online", "message": "FastAPI is running"}


@app.post("/api/sync")
def sync_tracks():
    response = s3_client.list_objects_v2(Bucket=R2_BUCKET_NAME)

    with get_connection() as connection:
        for item in response.get("Contents", []):
            key = item["Key"]
            if not key.lower().endswith((".mp3", ".m4a")):
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