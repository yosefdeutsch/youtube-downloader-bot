import os, uuid, threading, subprocess, zipfile, re
from flask import Flask, request, jsonify, send_file

def sanitize_filename(filename):
    """Remove only truly illegal filename characters, keep Hebrew and Unicode."""
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    if len(filename) > 200:
        filename = filename[:200]
    return filename.strip()

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

def split_video_file(input_path, work_dir, part_size_mb=38):
    part_size_bytes = part_size_mb * 1024 * 1024
    base            = os.path.splitext(os.path.basename(input_path))[0]

    # Get duration and file size
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
        bytes_per_second = file_size / duration
        segment_duration = int(part_size_bytes / bytes_per_second)
        segment_duration = max(10, min(segment_duration, 3600))
    else:
        segment_duration = 300

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

    temp_parts = sorted([
        os.path.join(work_dir, f)
        for f in os.listdir(work_dir)
        if f.startswith("temp_split_") and f.endswith(".mp4")
    ])

    if not temp_parts:
        return []

    # Rename and verify each part is under limit
    renamed = []
    for idx, p in enumerate(temp_parts):
        new_name = os.path.join(work_dir, f"{base}_part{str(idx+1).zfill(3)}.mp4")
        os.rename(p, new_name)
        # Log size for debugging
        part_size = os.path.getsize(new_name) / (1024*1024)
        renamed.append(new_name)

    return renamed

def run_download(job_id, url, cookies_content, format_id, custom_name, compress=False, audio_only=False):
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
                session = req_lib.Session()
                # Use gdown-style URL which handles auth automatically
                dl_url  = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t"
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                }
                response = session.get(dl_url, headers=headers, stream=True)
                with open(dl_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=1024*1024):
                        if chunk:
                            f.write(chunk)
                # Check it's not an HTML error page
                if os.path.getsize(dl_path) < 50000:
                    with open(dl_path, "rb") as f:
                        header = f.read(500)
                    if b"<html" in header.lower() or b"<!doc" in header.lower():
                        update_job(job_id, "error", f"❌ Google Drive blocked the download. Content: {header[:200]}")
                        return
                downloaded = dl_path
                # Convert to mp3 if audio only
                if audio_only:
                    update_job(job_id, "running", "Extracting audio to MP3…")
                    mp3_name = os.path.splitext(fname)[0] + ".mp3"
                    mp3_path = os.path.join(work_dir, mp3_name)
                    mp3_cmd  = [
                        "ffmpeg", "-i", dl_path,
                        "-vn",
                        "-acodec", "libmp3lame",
                        "-q:a", "0",
                        "-map_metadata", "0",
                        mp3_path, "-y"
                    ]
                    result = subprocess.run(mp3_cmd, capture_output=True, timeout=600)
                    if result.returncode == 0 and os.path.exists(mp3_path):
                        os.remove(dl_path)
                        downloaded = mp3_path
            except Exception as ex:
                update_job(job_id, "error", f"❌ Drive download failed: {str(ex)}")
                return

        else:
            if audio_only:
                fmt = format_id if format_id and format_id not in ["best", "bestaudio"] else "bestaudio[ext=m4a]/bestaudio/best"
            elif format_id and format_id != "best":
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
                if audio_only:
                    cmd = [
                        "yt-dlp", "--no-warnings",
                        "--extract-audio",
                        "--audio-format", "mp3",
                        "--audio-quality", "0",
                        "--embed-thumbnail",
                        "--add-metadata",
                        "--output", f"{work_dir}/%(id)s.%(ext)s",
                        "--print", "after_move:%(title)s\t%(id)s\t%(ext)s",
                    ]
                else:
                    cmd = [
                        "yt-dlp", "--no-warnings",
                        "--merge-output-format", "mp4",
                        "--output", f"{work_dir}/%(id)s.%(ext)s",
                        "--print", "after_move:%(title)s\t%(id)s\t%(ext)s",
                    ]
                if is_youtube:
                    cmd += ["--remote-components", "ejs:github"]
                cmd += extra_args
                # Only use cookies for YouTube
                if cookies_path and is_youtube:
                    cmd += ["--cookies", cookies_path]
                if is_m3u8:
                    cmd += ["--downloader", "ffmpeg", "--hls-prefer-ffmpeg"]
                cmd.append(url)

                result = subprocess.run(cmd, capture_output=True, text=True, timeout=3000)
                if result.returncode == 0:
                    # Parse real title from stdout
                    real_title = None
                    for line in result.stdout.splitlines():
                        if "\t" in line:
                            parts_line = line.split("\t")
                            if len(parts_line) >= 3:
                                real_title = parts_line[0].strip()
                                break

                    for fname in os.listdir(work_dir):
                        if fname.endswith((".mp4", ".mkv", ".webm", ".mp3")) and not fname.startswith("cookies"):
                            fpath = os.path.join(work_dir, fname)
                            if real_title:
                                ext      = os.path.splitext(fname)[1]
                                new_name = sanitize_filename(real_title) + ext
                                new_path = os.path.join(work_dir, new_name)
                                try:
                                    os.rename(fpath, new_path)
                                    downloaded = new_path
                                except:
                                    downloaded = fpath
                            else:
                                downloaded = fpath
                            break
                    if downloaded:
                        break
                last_error = result.stderr[-600:] if result.stderr else "Unknown error"

            if not downloaded:
                update_job(job_id, "error", f"yt-dlp error (all methods failed):\n{last_error[:300]}")
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
            ext          = ".mp3" if audio_only else ".mp4"
            # Store original custom name for Drive upload (supports Hebrew/Unicode)
            safe_name    = custom_name.replace(".mp4","").replace(".mp3","")
            new_path     = os.path.join(work_dir, safe_name + ext)
            try:
                os.rename(downloaded, new_path)
                downloaded = new_path
            except Exception:
                # If rename fails (e.g. filesystem doesn't support Unicode), keep original
                pass

        # Always split if over 40MB
        file_size = os.path.getsize(downloaded)
        update_job(job_id, "running", f"File size: {file_size // (1024*1024)}MB. Preparing…")
        # Don't split audio files
        if not audio_only and file_size > 40 * 1024 * 1024:
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
    audio_only  = data.get("audio_only", False)

    if secret != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    if not url:
        return jsonify({"error": "url is required"}), 400

    job_id = str(uuid.uuid4())
    update_job(job_id, "queued", "Job queued…", custom_name=("compressed" if compress and not custom_name else custom_name))
    threading.Thread(
        target=run_download,
        args=(job_id, url, cookies, format_id, custom_name, compress, audio_only),
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
    mimetype = "audio/mpeg" if fname.endswith(".mp3") else "video/mp4"
    return send_file(
        fpath,
        as_attachment=True,
        download_name=fname,
        mimetype=mimetype
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

@app.route("/filename/<job_id>/<int:index>")
def get_filename(job_id, index):
    if request.args.get("secret") != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 404
    files = job.get("file_paths", [])
    if index >= len(files):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"filename": os.path.basename(files[index])})

@app.route("/search", methods=["POST"])
def search_youtube():
    data            = request.get_json()
    secret          = data.get("secret", "")
    query           = data.get("query", "").strip()
    cookies_content = data.get("cookies_content", "")

    if secret != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    if not query:
        return jsonify({"error": "query is required"}), 400

    work_dir = "/tmp/search"
    os.makedirs(work_dir, exist_ok=True)

    cookies_path = None
    if cookies_content:
        cookies_path = f"{work_dir}/cookies.txt"
        with open(cookies_path, "w") as f:
            f.write(cookies_content)

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--skip-download",
        "--print", "%(id)s\t%(title)s\t%(channel)s\t%(duration_string)s\t%(upload_date)s\t%(thumbnail)s",
        "--playlist-end", "5",
        "--remote-components", "ejs:github",
        "--proxy", PROXY_URL,
        f"ytsearch5:{query}"
    ]
    if cookies_path:
        cmd += ["--cookies", cookies_path]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    results = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 6:
            vid_id    = parts[0].strip()
            title     = parts[1].strip()
            channel   = parts[2].strip()
            duration  = parts[3].strip()
            date      = parts[4].strip()
            thumbnail = parts[5].strip()

            if len(date) == 8:
                date = f"{date[6:8]}/{date[4:6]}/{date[0:4]}"

            results.append({
                "id":        vid_id,
                "url":       f"https://www.youtube.com/watch?v={vid_id}",
                "title":     title,
                "channel":   channel,
                "duration":  duration,
                "date":      date,
                "thumbnail": thumbnail
            })

    if not results:
        return jsonify({"error": "No results found", "stderr": result.stderr[-300:]}), 404

    return jsonify({"results": results})
            
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
    