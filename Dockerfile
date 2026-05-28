FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Always get latest yt-dlp at build time
RUN pip install --upgrade yt-dlp

COPY . .

CMD ["python", "-c", "import subprocess; subprocess.run(['pip', 'install', '--upgrade', 'yt-dlp']); exec(open('app.py').read())"]
