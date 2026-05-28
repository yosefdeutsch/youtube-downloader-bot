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

        # Try multiple strategies in order
        strategies = []

        if is_youtube:
            strategies = [
                # Strategy 1: mweb + proxy
                ["--extractor-args", "youtube:player_client=mweb",
                 "--format", "best[ext=mp4]/best",
                 "--proxy", os.environ.get("PROXY_URL", "")],
                # Strategy 2: tv_embedded + proxy
                ["--extractor-args", "youtube:player_client=tv_embedded",
                 "--format", "best",
                 "--proxy", os.environ.get("PROXY_URL", "")],
                # Strategy 3: android + proxy
                ["--extractor-args", "youtube:player_client=android",
                 "--format", "best[ext=mp4]/best",
                 "--proxy", os.environ.get("PROXY_URL", "")],
                # Strategy 4: mweb no proxy fallback
                ["--extractor-args", "youtube:player_client=mweb",
                 "--format", "best"],
            ]
        else:
            strategies = [
                ["--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"],
            ]

        last_error = ""
        for i, extra_args in enumerate(strategies):
            update_job(job_id, "running", f"Trying method {i+1} of {len(strategies)}…")

            cmd = [
                "yt-dlp",
                "--no-warnings",
                "--merge-output-format", "mp4",
                "--output", f"{work_dir}/%(title)s.%(ext)s",
            ] + extra_args

            if cookies_path:
                cmd += ["--cookies", cookies_path]
            if is_m3u8:
                cmd += ["--downloader", "ffmpeg", "--hls-prefer-ffmpeg"]

            cmd.append(url)

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3000)

            if result.returncode == 0:
                # Check if file was actually created
                for fname in os.listdir(work_dir):
                    if fname.endswith((".mp4", ".mkv", ".webm")) and not fname.startswith("cookies"):
                        fpath = os.path.join(work_dir, fname)
                        update_job(job_id, "done", f"✅ Ready: {fname}", fpath)
                        return

            last_error = result.stderr[-600:] if result.stderr else "Unknown error"

        update_job(job_id, "error", f"yt-dlp error (all methods failed):\n{last_error}")

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

@app.route("/formats", methods=["POST"])
def check_formats():
    data          = request.get_json()
    secret        = data.get("secret", "")
    url           = data.get("url", "")
    cookies_content = data.get("cookies_content", "")
    if secret != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    proxy = os.environ.get("PROXY_URL", "")
    work_dir = f"/tmp/formats_check"
    os.makedirs(work_dir, exist_ok=True)

    cookies_path = None
    if cookies_content:
        cookies_path = f"{work_dir}/cookies.txt"
        with open(cookies_path, "w") as f:
            f.write(cookies_content)

    cmd = ["yt-dlp", "--list-formats", "--proxy", proxy]
    if cookies_path:
        cmd += ["--cookies", cookies_path]
    cmd.append(url)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return jsonify({
        "stdout": result.stdout[-3000:],
        "stderr": result.stderr[-1000:]
    })
    
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
