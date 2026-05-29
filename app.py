import os, json, uuid, threading, subprocess, zipfile
from flask import Flask, request, jsonify, send_file
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)
API_SECRET   = os.environ.get("API_SECRET")
PROXY_URL    = os.environ.get("PROXY_URL", "")
SA_JSON      = os.environ.get("SERVICE_ACCOUNT_JSON")
SCOPES       = ["https://www.googleapis.com/auth/drive"]

jobs = {}

def update_job(job_id, status, message, file_paths=None, drive_links=None):
    jobs[job_id] = {
        "status":      status,
        "message":     message,
        "file_paths":  file_paths  or [],
        "drive_links": drive_links or []
    }

def get_drive_service():
    info  = json.loads(SA_JSON)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)

def upload_to_drive(fpath, folder_id):
    info  = json.loads(SA_JSON)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    drive = build("drive", "v3", credentials=creds)

    fname     = os.path.basename(fpath)
    file_meta = {
        "name":    fname,
        "parents": [folder_id]
    }

    media = MediaFileUpload(
        fpath,
        resumable=True,
        chunksize=5 * 1024 * 1024
    )

    request  = drive.files().create(
        body=file_meta,
        media_body=media,
        fields="id,name,webViewLink",
        supportsAllDrives=True
    )

    response = None
    while response is None:
        status, response = request.next_chunk()

    f = response
    drive.permissions().create(
        fileId=f["id"],
        body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True
    ).execute()

    return f["webViewLink"]

def quality_to_format(quality):
    mapping = {
        "best": "best",
        "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "720":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "480":  "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
        "360":  "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    }
    return mapping.get(quality, "best")

def split_video_file(input_path, work_dir, part_size_mb=45):
    part_size_bytes = part_size_mb * 1024 * 1024
    base            = os.path.splitext(os.path.basename(input_path))[0]
    output_pattern  = os.path.join(work_dir, f"{base}_part%03d.mp4")
    cmd = [
        "ffmpeg", "-i", input_path,
        "-c", "copy",
        "-f", "segment",
        "-segment_size", str(part_size_bytes),
        "-reset_timestamps", "1",
        output_pattern, "-y"
    ]
    subprocess.run(cmd, capture_output=True, timeout=600)
    parts = sorted([
        os.path.join(work_dir, f)
        for f in os.listdir(work_dir)
        if f.startswith(base + "_part") and f.endswith(".mp4")
    ])
    return parts

def run_download(job_id, url, cookies_content, format_id, folder_id, custom_name):
    try:
        update_job(job_id, "running", "Starting download…")
        work_dir = f"/tmp/{job_id}"
        os.makedirs(work_dir, exist_ok=True)

        cookies_path = None
        if cookies_content:
            cookies_path = f"{work_dir}/cookies.txt"
            with open(cookies_path, "w") as f:
                f.write(cookies_content)

        is_youtube = "youtube.com" in url or "youtu.be" in url
        is_m3u8    = ".m3u8" in url

        # Build format selector
        if format_id and format_id != "best":
            fmt = format_id
        elif is_youtube:
            fmt = "bestvideo[vcodec^=av01]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        else:
            fmt = "bestvideo+bestaudio/best"

        if is_youtube:
            strategies = [
                ["--extractor-args", "youtube:player_client=tv_embedded",
                 "--format", fmt, "--proxy", PROXY_URL],
                ["--extractor-args", "youtube:player_client=mweb",
                 "--format", fmt, "--proxy", PROXY_URL],
                ["--extractor-args", "youtube:player_client=tv_embedded",
                 "--format", "best", "--proxy", PROXY_URL],
            ]
        else:
            strategies = [
                ["--format", fmt],
                ["--format", "best[ext=mp4]/best"],
                ["--format", "best"],
            ]

        last_error = ""
        downloaded = None

        for i, extra_args in enumerate(strategies):
            update_job(job_id, "running", f"Trying method {i+1} of {len(strategies)}…")
            cmd = [
                "yt-dlp", "--no-warnings",
                "--merge-output-format", "mp4",
                "--remote-components", "ejs:github",
                "--restrict-filenames",
                "--output", f"{work_dir}/%(title).100B.%(ext)s",
            ] + extra_args
            if cookies_path:
                cmd += ["--cookies", cookies_path]
            if is_m3u8:
                cmd += ["--downloader", "ffmpeg", "--hls-prefer-ffmpeg"]
            cmd.append(url)

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3000)
            if result.returncode == 0:
                for fname in os.listdir(work_dir):
                    if fname.endswith((".mp4", ".mkv", ".webm")) and not fname.startswith("cookies"):
                        downloaded = os.path.join(work_dir, fname)
                        break
                if downloaded:
                    break
            last_error = result.stderr[-600:] if result.stderr else "Unknown error"

        if not downloaded:
            update_job(job_id, "error", f"yt-dlp error (all methods failed):\n{last_error}")
            return

        # Apply custom name if provided
        if custom_name:
            new_path = os.path.join(work_dir, custom_name.replace(".mp4","") + ".mp4")
            os.rename(downloaded, new_path)
            downloaded = new_path

        # Split into 45MB parts
        update_job(job_id, "running", "Preparing file(s) for upload…")
        if os.path.getsize(downloaded) > 45 * 1024 * 1024:
            update_job(job_id, "running", "Splitting video into parts…")
            parts = split_video_file(downloaded, work_dir)
            if parts:
                os.remove(downloaded)
                final_files = parts
            else:
                final_files = [downloaded]
        else:
            final_files = [downloaded]

        # Upload each part to Drive separately
        drive_links = []
        for idx, fpath in enumerate(final_files):
            update_job(job_id, "running", f"Uploading part {idx+1} of {len(final_files)} to Drive…")
            try:
                link = upload_to_drive(fpath, folder_id)
                drive_links.append({"name": os.path.basename(fpath), "link": link})
            except Exception as e:
                import traceback
                update_job(job_id, "error", f"❌ Upload failed for {os.path.basename(fpath)}: {str(e)}\n{traceback.format_exc()[-500:]}")
                return

        msg = "✅ Saved to Drive!\n\n"
        for f in drive_links:
            msg += f"📁 {f['name']}\n🔗 {f['link']}\n\n"
        if len(drive_links) > 1:
            msg += f"📦 {len(drive_links)} parts uploaded — play them in order."

        update_job(job_id, "done", msg, final_files, drive_links)

    except subprocess.TimeoutExpired:
        update_job(job_id, "error", "❌ Timeout: video took too long.")
    except Exception as e:
        update_job(job_id, "error", f"❌ Unexpected error: {str(e)}")

# ── Routes ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return jsonify({"status": "Video Downloader Bot is running 🎬"})

@app.route("/download", methods=["POST"])
def start_download():
    data      = request.get_json()
    secret    = data.get("secret", "")
    url       = data.get("url", "").strip()
    cookies   = data.get("cookies_content", "").strip()
    format_id = data.get("format_id", "best")
    folder_id = data.get("folder_id", "")
    custom_name = data.get("custom_name", "").strip()

    if secret != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    if not url:
        return jsonify({"error": "url is required"}), 400

    job_id = str(uuid.uuid4())
    update_job(job_id, "queued", "Job queued…")
    threading.Thread(
        target=run_download,
        args=(job_id, url, cookies, format_id, folder_id, custom_name),
        daemon=True
    ).start()
    return jsonify({"job_id": job_id}), 202

@app.route("/status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status":      job["status"],
        "message":     job["message"],
        "drive_links": job.get("drive_links", [])
    })

@app.route("/result_zip/<job_id>")
def result_zip(job_id):
    if request.args.get("secret") != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 404

    work_dir = f"/tmp/{job_id}"
    try:
        actual_files = sorted([
            os.path.join(work_dir, f)
            for f in os.listdir(work_dir)
            if f.endswith((".mp4", ".mkv", ".webm"))
            and not f.startswith("cookies")
        ])
    except:
        return jsonify({"error": "Files no longer on disk"}), 404

    if not actual_files:
        return jsonify({"error": "No files found"}), 404

    # Auto-split if over 45MB
    final_files = []
    for fpath in actual_files:
        if "_part" not in fpath and os.path.getsize(fpath) > 45 * 1024 * 1024:
            parts = split_video_file(fpath, work_dir)
            if parts:
                os.remove(fpath)
                final_files.extend(parts)
            else:
                final_files.append(fpath)
        else:
            final_files.append(fpath)

    if len(final_files) == 1:
        fpath = final_files[0]
        return send_file(fpath, as_attachment=True, download_name=os.path.basename(fpath))

    zip_path = os.path.join(work_dir, "parts.zip")
    with zipfile.ZipFile(zip_path, "w") as zf:
        for p in final_files:
            zf.write(p, os.path.basename(p))
    return send_file(zip_path, as_attachment=True, download_name="video_parts.zip")

@app.route("/formats", methods=["POST"])
def check_formats():
    data            = request.get_json()
    secret          = data.get("secret", "")
    url             = data.get("url", "")
    cookies_content = data.get("cookies_content", "")
    if secret != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    work_dir = "/tmp/formats_check"
    os.makedirs(work_dir, exist_ok=True)
    cookies_path = None
    if cookies_content:
        cookies_path = f"{work_dir}/cookies.txt"
        with open(cookies_path, "w") as f:
            f.write(cookies_content)
    cmd = ["yt-dlp", "--list-formats", "--remote-components", "ejs:github", "--proxy", PROXY_URL]
    if cookies_path:
        cmd += ["--cookies", cookies_path]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return jsonify({"stdout": result.stdout[-3000:], "stderr": result.stderr[-1000:]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
