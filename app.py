import os, json, uuid, threading, subprocess
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

API_SECRET = os.environ.get("API_SECRET")

# ── In-memory job store ────────────────────────────────────────────────────
jobs = {}  # job_id → { status, message, file_path }

def update_job(job_id, status, message, file_path=None):
    jobs[job_id] = {"status": status, "message": message, "file_path": file_path}

# ── Background download ────────────────────────────────────────────────────
def run_download(job_id, url, cookies_content):
    try:
        update_job(job_id, "running", "Updating yt-dlp…")
        update_job(job_id, "running", "Starting download…")
        work_dir = f"/tmp/{job_id}"
        os.makedirs(work_dir, exist_ok=True)

        cookies_path = None
        if cookies_content:
            cookies_path = f"{work_dir}/cookies.txt"
            with open(cookies_path, "w") as f:
                f.write(cookies_content)

        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--merge-output-format", "mp4",
            "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            "--extractor-args", "youtube:player_client=tv_embedded",
            "--output", f"{work_dir}/%(title)s.%(ext)s",
        ]
        if cookies_path:
            cmd += ["--cookies", cookies_path]
        if ".m3u8" in url:
            cmd += ["--downloader", "ffmpeg", "--hls-prefer-ffmpeg"]

        cmd.append(url)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3000)

        if result.returncode != 0:
            update_job(job_id, "error", f"yt-dlp error: {result.stderr[-800:]}")
            return

        # Find the downloaded file
        for fname in os.listdir(work_dir):
            if fname.endswith((".mp4", ".mkv", ".webm")) and not fname.startswith("cookies"):
                fpath = os.path.join(work_dir, fname)
                update_job(job_id, "done", f"✅ Ready: {fname}", fpath)
                return

        update_job(job_id, "error", "Download finished but no video file found.")

    except subprocess.TimeoutExpired:
        update_job(job_id, "error", "❌ Timeout: video took too long.")
    except Exception as e:
        update_job(job_id, "error", f"❌ Unexpected error: {str(e)}")

# ── Routes ─────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "Video Downloader Bot is running 🎬"})

@app.route("/download", methods=["POST"])
def start_download():
    data    = request.get_json()
    url     = data.get("url", "").strip()
    cookies = data.get("cookies_content", "").strip()
    secret  = data.get("secret", "")

    if secret != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    if not url:
        return jsonify({"error": "url is required"}), 400

    job_id = str(uuid.uuid4())
    update_job(job_id, "queued", "Job queued…")
    threading.Thread(target=run_download, args=(job_id, url, cookies), daemon=True).start()
    return jsonify({"job_id": job_id}), 202

@app.route("/status/<job_id>", methods=["GET"])
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    # Don't expose file_path to client
    return jsonify({"status": job["status"], "message": job["message"]})

@app.route("/result/<job_id>", methods=["GET"])
def get_result(job_id):
    secret = request.args.get("secret", "")
    if secret != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 404
    fpath = job["file_path"]
    fname = os.path.basename(fpath)
    return send_file(fpath, as_attachment=True, download_name=fname)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
