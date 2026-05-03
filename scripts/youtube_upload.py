import argparse
import io
import json
import os
import sys
from pathlib import Path

# Windows terminali emoji içeren başlıkları yazdıramayabilir
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT_SECRETS_FILE = "client_secrets.json"
TOKEN_FILE = "token.json"


def authenticate():
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRETS_FILE):
                print(f"Error: {CLIENT_SECRETS_FILE} not found.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return creds


def upload_video(
    file_path,
    title,
    description="",
    tags=None,
    category_id="10",
    privacy_status="private",
):
    if not os.path.exists(file_path):
        print(f"Error: Video file not found: {file_path}")
        sys.exit(1)

    creds = authenticate()
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
        },
    }

    media = MediaFileUpload(file_path, chunksize=-1, resumable=True)

    print(f"Uploading: {file_path}")
    print(f"Title: {title}")
    print(f"Privacy: {privacy_status}")

    # Find the latest session output dir to write upload_progress.json
    progress_file = None
    output_dir = Path(__file__).resolve().parent.parent / "output"
    if output_dir.exists():
        session_dirs = sorted(output_dir.glob("session_*"), reverse=True)
        if session_dirs:
            progress_file = session_dirs[0] / "upload_progress.json"

    def _write_progress(pct: int, status_str: str, video_id: str = None):
        if progress_file:
            import json as _json
            data = {"percent": pct, "status": status_str}
            if video_id:
                data["video_id"] = video_id
                data["url"] = f"https://www.youtube.com/watch?v={video_id}"
            progress_file.write_text(_json.dumps(data), encoding="utf-8")

    try:
        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        _write_progress(0, "uploading")
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                print(f"Progress: {progress}%", end="\r")
                _write_progress(progress, "uploading")

        video_id = response["id"]
        print(f"\nUpload complete!")
        print(f"Video URL: https://www.youtube.com/watch?v={video_id}")
        _write_progress(100, "done", video_id)
        return video_id

    except HttpError as e:
        print(f"\nError during upload: {e}")
        if progress_file:
            _write_progress(0, f"error: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Upload a video to YouTube")
    parser.add_argument("--file", required=True, help="Path to the video file")
    parser.add_argument("--metadata", default=None, help="Path to metadata.json (overrides other flags)")
    parser.add_argument("--title", default=None, help="Video title")
    parser.add_argument("--description", default="", help="Video description")
    parser.add_argument("--tags", nargs="+", default=[], help="Video tags (space-separated)")
    parser.add_argument("--category-id", default="10", help="YouTube category ID (default: 10 = Music)")
    parser.add_argument(
        "--privacy",
        default="private",
        choices=["private", "unlisted", "public"],
        help="Privacy status (default: private)",
    )

    args = parser.parse_args()

    if args.metadata:
        import json
        with open(args.metadata, "r", encoding="utf-8") as f:
            meta = json.load(f)
        upload_video(
            file_path=args.file,
            title=meta["title"],
            description=meta.get("description", ""),
            tags=meta.get("tags", []),
            category_id=meta.get("category_id", "10"),
            privacy_status=meta.get("privacy_status", "private"),
        )
    else:
        if not args.title:
            print("Error: --title is required when --metadata is not provided.")
            sys.exit(1)
        upload_video(
            file_path=args.file,
            title=args.title,
            description=args.description,
            tags=args.tags,
            category_id=args.category_id,
            privacy_status=args.privacy,
        )


if __name__ == "__main__":
    main()
