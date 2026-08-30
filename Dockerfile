FROM python:3.12-slim
WORKDIR /app

# Install ffmpeg untuk video conversion
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY app.py .

# Port
ENV PORT=3000
EXPOSE 3000

CMD ["gunicorn", "-b", "0.0.0.0:3000", "-w", "2", "--worker-class", "gthread", "--threads", "8", "--timeout", "300", "app:app"]
