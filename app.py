import os, json, uuid, threading, subprocess
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)

# ── Load service account from env var ──────────────────────────────────────
SA_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")
SCOPES  = ["https://www.googleapis.com/auth/drive"]

def get_drive_service():
    info  = json.loads(SA_JSON)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)

# ── In-memory job store ────────────────────────────────────────────────────
jobs = {}   # job_id → { status, message, files }

def update_job(job_id, status, message, files=None):
    jobs[job_id] = {"status": status, "message": message, "files": files or []}

# ── Background download + upload ───────────────────────────────────────────
def run_download(job_id, url, folder_id, cookies_file_id):
    try:
        update_job(job_id, "running", "Starting download…")
        work_dir = f"/tmp/{job_id}"
        os.makedirs(work_dir, exist_ok=True)

        cookies_path = None
        if cookies_file_id:
            cookies_path = f"{work_dir}/cookies.txt"
            download_from_drive(cookies_file_id, cookies_path)

        # Build yt-dlp command
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--merge-output-format", "mp4",
            "--output", f"{work_dir}/%(title)s.%(ext)s",
        ]
        if cookies_path:
            cmd += ["--cookies", cookies_path]

        # m3u8 direct link handling
        if ".m3u8" in url:
            cmd += ["--downloader", "ffmpeg", "--hls-prefer-ffmpeg"]

        cmd.append(url)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3000)

        if result.returncode != 0:
            update_job(job_id, "error", f"yt-dlp error:\n{result.stderr[-1000:]}")
            return

        # Upload all mp4 files to Drive
        drive = get_drive_service()
        uploaded = []
        for fname in os.listdir(work_dir):
            if fname.endswith((".mp4", ".mkv", ".webm")) and not fname.startswith("cookies"):
                fpath = os.path.join(work_dir, fname)
                file_meta = {"name": fname, "parents": [folder_id]}
                media = MediaFileUpload(fpath, resumable=True)
                f = drive.files().create(body=file_meta, media_body=media, fields="id,name,webViewLink").execute()
                uploaded.append({"name": f["name"], "link": f["webViewLink"]})
                os.remove(fpath)

        if uploaded:
            update_job(job_id, "done", f"✅ Uploaded {len(uploaded)} file(s) to Drive.", uploaded)
        else:
            update_job(job_id, "error", "Download finished but no video files found.")

    except subprocess.TimeoutExpired:
        update_job(job_id, "error", "❌ Timeout: video took too long to download.")
    except Exception as e:
        update_job(job_id, "error", f"❌ Unexpected error: {str(e)}")

def download_from_drive(file_id, dest_path):
    """Download a file from Drive by ID (used for cookies.txt)."""
    drive = get_drive_service()
    request = drive.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as f:
        f.write(request.execute())

# ── Routes ─────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "Video Downloader Bot is running 🎬"})

@app.route("/download", methods=["POST"])
def start_download():
    data      = request.get_json()
    url       = data.get("url", "").strip()
    folder_id = data.get("folder_id", "").strip()
    cookies   = data.get("cookies_file_id", "").strip()  # optional
    secret    = data.get("secret", "")

    if secret != os.environ.get("API_SECRET"):
        return jsonify({"error": "Unauthorized"}), 401
    if not url or not folder_id:
        return jsonify({"error": "url and folder_id are required"}), 400

    job_id = str(uuid.uuid4())
    update_job(job_id, "queued", "Job queued…")
    threading.Thread(target=run_download, args=(job_id, url, folder_id, cookies), daemon=True).start()
    return jsonify({"job_id": job_id}), 202

@app.route("/status/<job_id>", methods=["GET"])
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
