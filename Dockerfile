FROM python:3.12-slim
WORKDIR /app

# Install ffmpeg untuk video conversion (diperlukan yt-dlp merge streams ke MP4)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY app.py .

# Port (Render set PORT env otomatis; default 3000 untuk lokal)
ENV PORT=3000
EXPOSE 3000

# Render free tier = 0.1 CPU. -w 1 + 4 thread gthread = 4 concurrent job maks,
# hindari thrashing/context-switch overhead. yt-dlp fork subprocess jadi thread aman.
CMD ["gunicorn", "-b", "0.0.0.0:3000", "-w", "1", "--worker-class", "gthread", \
     "--threads", "4", "--timeout", "300", "app:app"]
