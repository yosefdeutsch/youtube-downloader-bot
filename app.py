import os, uuid, threading, subprocess, zipfile
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)
API_SECRET = os.environ.get("API_SECRET")
PROXY_URL  = os.environ.get("PROXY_URL", "")

jobs = {}

def update_job(job_id, status, message, file_paths=None, custom_name=""):
    existing = jobs.get(job_id, {})
    jobs[job_id] = {
        "status":      status,
        "message":     message,
        "file_paths":  file_paths or existing.get("file_paths", []),
        "custom_name": custom_name or existing.get("custom_name", "")
    }

def split_video_file(input_path, work_dir, part_size_mb=40):
    part_size_bytes = part_size_mb * 1024 * 1024
    base            = os.path.splitext(os.path.basename(input_path))[0]

    # Get duration
    probe = subprocess.run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        input_path
    ], capture_output=True, text=True)

    try:
        duration = float(probe.stdout.strip())
    except:
        duration = 0

    file_size = os.path.getsize(input_path)

    if duration > 0 and file_size > 0:
        segment_duration = int((part_size_bytes / file_size) * duration)
        segment_duration = max(30, segment_duration)
    else:
        segment_duration = 600

    # Use temp prefix to avoid name conflicts during rename
    temp_pattern = os.path.join(work_dir, f"temp_split_%03d.mp4")

    cmd = [
        "ffmpeg", "-i", input_path,
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(segment_duration),
        "-reset_timestamps", "1",
        "-avoid_negative_ts", "make_zero",
        temp_pattern, "-y"
    ]
    subprocess.run(cmd, capture_output=True, timeout=600)

    # Find all temp split files
    temp_parts = sorted([
        os.path.join(work_dir, f)
        for f in os.listdir(work_dir)
        if f.startswith("temp_split_") and f.endswith(".mp4")
    ])

    if not temp_parts:
        return []

    # Rename to final names starting at 001
    renamed = []
    for idx, p in enumerate(temp_parts):
        new_name = os.path.join(work_dir, f"{base}_part{str(idx+1).zfill(3)}.mp4")
        os.rename(p, new_name)
        renamed.append(new_name)

    return renamed

def run_download(job_id, url, cookies_content, format_id, custom_name, compress=False):
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
        downloaded = None

        # Handle Google Drive directly
        if "drive.google.com" in url:
            import re, requests as req_lib
            file_id_match = re.search(r'/file/d/([-\w]+)', url)
            if not file_id_match:
                update_job(job_id, "error", "❌ Could not extract file ID from Drive link.")
                return
            file_id = file_id_match.group(1)
            fname   = custom_name.replace(".mp4","") + ".mp4" if custom_name else f"{file_id}.mp4"
            dl_path = os.path.join(work_dir, fname)
            update_job(job_id, "running", "Downloading from Google Drive…")
            try:
                session  = req_lib.Session()
                dl_url   = f"https://drive.google.com/uc?export=download&id={file_id}"
                response = session.get(dl_url, stream=True)
                token    = None
                for key, value in response.cookies.items():
                    if key.startswith("download_warning"):
                        token = value
                if token:
                    response = session.get(dl_url + f"&confirm={token}", stream=True)
                with open(dl_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=1024*1024):
                        if chunk:
                            f.write(chunk)
                if os.path.getsize(dl_path) < 10000:
                    with open(dl_path, "rb") as f:
                        if b"<html" in f.read(200).lower():
                            update_job(job_id, "error", "❌ Google Drive blocked the download. Make sure the file is shared as 'Anyone with the link'.")
                            return
                downloaded = dl_path
            except Exception as ex:
                update_job(job_id, "error", f"❌ Drive download failed: {str(ex)}")
                return

        else:
            if format_id and format_id != "best":
                fmt = f"{format_id}+bestaudio[ext=m4a]/{format_id}+bestaudio/{format_id}"
            elif is_youtube:
                fmt = "bestvideo[vcodec^=av01][filesize<400M]+bestaudio[ext=m4a]/bestvideo[filesize<400M]+bestaudio/best[filesize<400M]/best"
            else:
                fmt = "bestvideo[filesize<400M]+bestaudio/best[filesize<400M]/best"

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
                    ["--format", "best[ext=mp4]/best"],
                    ["--format", "best"],
                    [],
                ]

            last_error = ""
            for i, extra_args in enumerate(strategies):
                update_job(job_id, "running", f"Trying method {i+1} of {len(strategies)}…")
                cmd = [
                    "yt-dlp", "--no-warnings",
                    "--merge-output-format", "mp4",
                    "--restrict-filenames",
                    "--output", f"{work_dir}/%(title).100B.%(ext)s",
                ]
                if is_youtube:
                    cmd += ["--remote-components", "ejs:github"]
                cmd += extra_args
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

        # Compress if needed
        if compress:
            update_job(job_id, "running", "Compressing video…")
            base_name       = custom_name if custom_name else os.path.splitext(os.path.basename(downloaded))[0]
            compressed_name = base_name.replace(".mp4","") + "_compressed.mp4"
            compressed_path = os.path.join(work_dir, compressed_name)
            compress_cmd = [
                "ffmpeg", "-i", downloaded,
                "-vcodec", "libx264",
                "-crf", "28",
                "-preset", "fast",
                "-acodec", "aac",
                "-b:a", "128k",
                compressed_path, "-y"
            ]
            result = subprocess.run(compress_cmd, capture_output=True, timeout=3600)
            if result.returncode == 0 and os.path.exists(compressed_path):
                os.remove(downloaded)
                downloaded  = compressed_path
                custom_name = compressed_name

        # Apply custom name
        if custom_name and not compress:
            new_path = os.path.join(work_dir, custom_name.replace(".mp4","") + ".mp4")
            os.rename(downloaded, new_path)
            downloaded = new_path

        # Always split if over 40MB
        file_size = os.path.getsize(downloaded)
        update_job(job_id, "running", f"File size: {file_size // (1024*1024)}MB. Preparing…")
        if file_size > 40 * 1024 * 1024:
            update_job(job_id, "running", "Splitting video into parts…")
            parts = split_video_file(downloaded, work_dir)
            if parts and len(parts) > 0:
                os.remove(downloaded)
                final_files = [f for f in parts if os.path.exists(f)]
            else:
                final_files = [downloaded] if os.path.exists(downloaded) else []
        else:
            final_files = [downloaded] if os.path.exists(downloaded) else []

        if not final_files:
            update_job(job_id, "error", "❌ No files found after processing.")
            return

        update_job(job_id, "done", f"✅ Ready: {len(final_files)} part(s)", final_files)

    except subprocess.TimeoutExpired:
        update_job(job_id, "error", "❌ Timeout: video took too long.")
    except Exception as e:
        update_job(job_id, "error", f"❌ Unexpected error: {str(e)}")

@app.route("/")
def index():
    return jsonify({"status": "Video Downloader Bot is running 🎬"})

@app.route("/download", methods=["POST"])
def start_download():
    data        = request.get_json()
    secret      = data.get("secret", "")
    url         = data.get("url", "").strip()
    cookies     = data.get("cookies_content", "").strip()
    format_id   = data.get("format_id", "best")
    custom_name = data.get("custom_name", "").strip()
    compress    = data.get("compress", False)

    if secret != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    if not url:
        return jsonify({"error": "url is required"}), 400

    job_id = str(uuid.uuid4())
    update_job(job_id, "queued", "Job queued…", custom_name=("compressed" if compress and not custom_name else custom_name))
    threading.Thread(
        target=run_download,
        args=(job_id, url, cookies, format_id, custom_name, compress),
        daemon=True
    ).start()
    return jsonify({"job_id": job_id}), 202

@app.route("/status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status":   job["status"],
        "message":  job["message"],
        "parts":    len(job.get("file_paths", [])),
        "custom_name": job.get("custom_name", "")
    })

@app.route("/part/<job_id>/<int:index>")
def get_part(job_id, index):
    if request.args.get("secret") != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 404
    files = job.get("file_paths", [])
    if index >= len(files):
        return jsonify({"error": "Part not found"}), 404
    fpath = files[index]
    if not os.path.exists(fpath):
        return jsonify({"error": "File no longer on disk"}), 404
    fname = os.path.basename(fpath)
    return send_file(
        fpath,
        as_attachment=True,
        download_name=fname,
        mimetype="video/mp4"
    )

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

@app.route("/debug/<job_id>")
def debug_job(job_id):
    if request.args.get("secret") != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    work_dir = f"/tmp/{job_id}"
    job = jobs.get(job_id)
    try:
        files = os.listdir(work_dir)
        sizes = {f: os.path.getsize(os.path.join(work_dir, f)) for f in files}
    except:
        files = ["FOLDER NOT FOUND"]
        sizes = {}
    return jsonify({
        "job":        job,
        "files":      files,
        "sizes_mb":   {k: round(v/1024/1024, 2) for k,v in sizes.items()}
    })

@app.route("/part_ready/<job_id>/<int:index>")
def part_ready(job_id, index):
    if request.args.get("secret") != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    job = jobs.get(job_id)
    if not job:
        return jsonify({"ready": False, "status": "not_found"})
    files = job.get("file_paths", [])
    if index < len(files) and os.path.exists(files[index]):
        return jsonify({"ready": True, "total": len(files), "job_status": job["status"]})
    return jsonify({"ready": False, "total": len(files), "job_status": job["status"]})
    
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)