import os, json, uuid, threading, subprocess
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)
API_SECRET = os.environ.get("API_SECRET")
PROXY_URL  = os.environ.get("PROXY_URL", "")

jobs = {}

def update_job(job_id, status, message, file_paths=None):
    jobs[job_id] = {"status": status, "message": message, "file_paths": file_paths or []}

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

def run_download(job_id, url, cookies_content, quality, do_split):
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
        fmt        = quality_to_format(quality)

        if is_youtube:
            strategies = [
                ["--extractor-args", "youtube:player_client=tv_embedded",
                 "--format", fmt, "--proxy", PROXY_URL],
                ["--extractor-args", "youtube:player_client=mweb",
                 "--format", "best[ext=mp4]/best", "--proxy", PROXY_URL],
                ["--extractor-args", "youtube:player_client=tv_embedded",
                 "--format", "best", "--proxy", PROXY_URL],
                ["--extractor-args", "youtube:player_client=tv_embedded",
                 "--format", "18", "--proxy", PROXY_URL],
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
                "yt-dlp",
                "--no-warnings",
                "--merge-output-format", "mp4",
                "--remote-components", "ejs:github",
                "--output", f"{work_dir}/%(title)s.%(ext)s",
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

        # Split or keep as single file
        if do_split:
            update_job(job_id, "running", "Splitting video into parts…")
            if os.path.getsize(downloaded) > 45 * 1024 * 1024:
                parts = split_video_file(downloaded, work_dir)
                os.remove(downloaded)
                final_files = parts if parts else [downloaded]
            else:
                final_files = [downloaded]
        else:
            final_files = [downloaded]

        update_job(job_id, "done", f"✅ Ready: {len(final_files)} file(s)", final_files)

    except subprocess.TimeoutExpired:
        update_job(job_id, "error", "❌ Timeout: video took too long.")
    except Exception as e:
        update_job(job_id, "error", f"❌ Unexpected error: {str(e)}")

# ── Routes ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return jsonify({"status": "Video Downloader Bot is running 🎬"})

@app.route("/download", methods=["POST"])
def start_download():
    data     = request.get_json()
    secret   = data.get("secret", "")
    url      = data.get("url", "").strip()
    cookies  = data.get("cookies_content", "").strip()
    quality  = data.get("quality", "best")
    do_split = data.get("split_video", "no") == "yes"

    if secret != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    if not url:
        return jsonify({"error": "url is required"}), 400

    job_id = str(uuid.uuid4())
    update_job(job_id, "queued", "Job queued…")
    threading.Thread(target=run_download, args=(job_id, url, cookies, quality, do_split), daemon=True).start()
    return jsonify({"job_id": job_id}), 202

@app.route("/status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"status": job["status"], "message": job["message"]})

# ── kept for backwards compatibility ──────────────────────────────────────
@app.route("/result/<job_id>")
def get_result(job_id):
    if request.args.get("secret") != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 404
    fpath = job["file_paths"][0] if job["file_paths"] else None
    if not fpath:
        return jsonify({"error": "No file"}), 404
    return send_file(fpath, as_attachment=True, download_name=os.path.basename(fpath))

# ── new multi-file endpoints ───────────────────────────────────────────────
@app.route("/result_info/<job_id>")
def result_info(job_id):
    if request.args.get("secret") != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 404
    return jsonify({"files": [os.path.basename(p) for p in job["file_paths"]]})

@app.route("/result_file/<job_id>/<filename>")
def result_file(job_id, filename):
    if request.args.get("secret") != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 404
    for fpath in job["file_paths"]:
        if os.path.basename(fpath) == filename:
            return send_file(fpath, as_attachment=True, download_name=filename)
    return jsonify({"error": "File not found"}), 404

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
