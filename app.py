```python
from flask import Flask, request, jsonify, send_file
import subprocess
import requests
import os
import uuid
import io
import logging
import re
from urllib.parse import urlparse

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB per downloaded file
DOWNLOAD_TIMEOUT = (15, 300)       # connect, read timeout
MAX_DOWNLOAD_RETRIES = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# HELPERS
# ============================================================

def valid_url(url):
    """Basic HTTP/HTTPS URL validation."""
    if not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def safe_filename(value):
    """Prevent unsafe filename characters."""
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", value)


def download_file(url, destination):
    """
    Download remote file with retries and streaming.
    Prevents loading the entire file into RAM.
    """

    if not valid_url(url):
        raise ValueError(f"Invalid URL: {url}")

    last_error = None

    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):

        try:
            logger.info(
                f"Downloading file (attempt {attempt}/{MAX_DOWNLOAD_RETRIES})"
            )

            with requests.get(
                url,
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
                headers={
                    "User-Agent": "FFmpeg-Merge-Service/1.0"
                }
            ) as response:

                response.raise_for_status()

                content_length = response.headers.get("content-length")

                if content_length:
                    if int(content_length) > MAX_FILE_SIZE:
                        raise ValueError(
                            "Remote file exceeds maximum allowed size"
                        )

                downloaded = 0

                with open(destination, "wb") as file:

                    for chunk in response.iter_content(
                        chunk_size=1024 * 1024
                    ):

                        if not chunk:
                            continue

                        downloaded += len(chunk)

                        if downloaded > MAX_FILE_SIZE:
                            raise ValueError(
                                "Downloaded file exceeds maximum allowed size"
                            )

                        file.write(chunk)

            logger.info(
                f"Download completed: {downloaded / (1024 * 1024):.2f} MB"
            )

            return

        except Exception as e:

            last_error = e

            logger.warning(
                f"Download failed on attempt {attempt}: {e}"
            )

            if os.path.exists(destination):
                os.remove(destination)

    raise RuntimeError(
        f"Download failed after {MAX_DOWNLOAD_RETRIES} attempts: {last_error}"
    )


def run_ffmpeg(command):
    """
    Run FFmpeg and return useful error information.
    """

    logger.info("Running FFmpeg")

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:

        logger.error("FFmpeg failed")

        logger.error(result.stderr[-5000:])

        raise RuntimeError(
            "FFmpeg processing failed:\n"
            + result.stderr[-3000:]
        )

    return result


def cleanup_files(files):
    """Delete temporary files safely."""

    for file in files:

        try:

            if file and os.path.exists(file):
                os.remove(file)
                logger.info(f"Deleted temporary file: {file}")

        except Exception as e:

            logger.warning(
                f"Could not delete temporary file {file}: {e}"
            )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "success": True,
        "service": "FFmpeg Video + Audio Merge Service",
        "status": "running",
        "version": "2.0"
    })


@app.route("/health", methods=["GET"])
def health():

    return jsonify({
        "success": True,
        "status": "healthy"
    })


# ============================================================
# VIDEO + AUDIO MERGE
# ============================================================

@app.route("/merge", methods=["POST"])
def merge():

    data = request.get_json(silent=True) or {}

    video_url = data.get("video_url")
    audio_url = data.get("audio_url")

    if not video_url or not audio_url:

        return jsonify({
            "success": False,
            "error": "video_url and audio_url are required"
        }), 400

    if not valid_url(video_url):

        return jsonify({
            "success": False,
            "error": "Invalid video_url"
        }), 400

    if not valid_url(audio_url):

        return jsonify({
            "success": False,
            "error": "Invalid audio_url"
        }), 400

    job_id = str(uuid.uuid4())

    video_file = f"/tmp/{job_id}_video.mp4"
    audio_file = f"/tmp/{job_id}_audio.mp3"
    output_file = f"/tmp/{job_id}_merged.mp4"

    temp_files = [
        video_file,
        audio_file,
        output_file
    ]

    logger.info(f"Starting merge job: {job_id}")

    try:

        # ----------------------------------------------------
        # Download video
        # ----------------------------------------------------

        download_file(
            video_url,
            video_file
        )

        # ----------------------------------------------------
        # Download audio
        # ----------------------------------------------------

        download_file(
            audio_url,
            audio_file
        )

        # ----------------------------------------------------
        # First attempt: stream copy video
        # ----------------------------------------------------

        try:

            run_ffmpeg([
                "ffmpeg",
                "-y",

                "-i",
                video_file,

                "-i",
                audio_file,

                "-map",
                "0:v:0",

                "-map",
                "1:a:0",

                "-c:v",
                "copy",

                "-c:a",
                "aac",

                "-b:a",
                "192k",

                "-shortest",

                "-movflags",
                "+faststart",

                output_file
            ])

        except Exception:

            # ------------------------------------------------
            # Fallback: re-encode video
            # ------------------------------------------------

            logger.warning(
                "Stream copy failed. Starting re-encode fallback."
            )

            if os.path.exists(output_file):
                os.remove(output_file)

            run_ffmpeg([
                "ffmpeg",
                "-y",

                "-i",
                video_file,

                "-i",
                audio_file,

                "-map",
                "0:v:0",

                "-map",
                "1:a:0",

                "-c:v",
                "libx264",

                "-preset",
                "veryfast",

                "-crf",
                "23",

                "-pix_fmt",
                "yuv420p",

                "-c:a",
                "aac",

                "-b:a",
                "192k",

                "-shortest",

                "-movflags",
                "+faststart",

                output_file
            ])

        # ----------------------------------------------------
        # Return binary video
        # ----------------------------------------------------

        with open(output_file, "rb") as file:

            video_data = file.read()

        logger.info(
            f"Merge completed successfully: {job_id}"
        )

        return send_file(
            io.BytesIO(video_data),
            mimetype="video/mp4",
            as_attachment=True,
            download_name="segment_merged.mp4"
        )

    except Exception as e:

        logger.exception(
            f"Merge failed: {job_id}"
        )

        return jsonify({
            "success": False,
            "job_id": job_id,
            "error": str(e)
        }), 500

    finally:

        cleanup_files(temp_files)


# ============================================================
# CONCAT MULTIPLE VIDEOS
# ============================================================

@app.route("/concat", methods=["POST"])
def concat():

    data = request.get_json(silent=True) or {}

    # Support both:
    # {
    #   "video_urls": [...]
    # }
    #
    # and:
    #
    # {
    #   "videos": [...]
    # }

    video_urls = (
        data.get("video_urls")
        or data.get("videos")
    )

    if not video_urls:

        return jsonify({
            "success": False,
            "error": "video_urls must be provided"
        }), 400

    if not isinstance(video_urls, list):

        return jsonify({
            "success": False,
            "error": "video_urls must be a list"
        }), 400

    if len(video_urls) == 0:

        return jsonify({
            "success": False,
            "error": "video_urls cannot be empty"
        }), 400

    # Safety limit
    if len(video_urls) > 100:

        return jsonify({
            "success": False,
            "error": "Maximum 100 videos allowed per concat job"
        }), 400

    # Validate URLs

    for index, url in enumerate(video_urls):

        if not valid_url(url):

            return jsonify({
                "success": False,
                "error": f"Invalid video URL at index {index}"
            }), 400

    job_id = str(uuid.uuid4())

    local_files = []

    list_file = f"/tmp/{job_id}_concat.txt"

    output_file = f"/tmp/{job_id}_final.mp4"

    temp_files = [
        list_file,
        output_file
    ]

    logger.info(
        f"Starting concat job: {job_id}"
    )

    try:

        # ----------------------------------------------------
        # Download all videos
        # ----------------------------------------------------

        for index, url in enumerate(video_urls):

            local_path = (
                f"/tmp/{job_id}_{index}.mp4"
            )

            download_file(
                url,
                local_path
            )

            local_files.append(local_path)

        temp_files.extend(local_files)

        # ----------------------------------------------------
        # Create concat file
        # ----------------------------------------------------

        with open(list_file, "w") as file:

            for path in local_files:

                file.write(
                    f"file '{path}'\n"
                )

        # ----------------------------------------------------
        # First attempt: fast concat
        # ----------------------------------------------------

        try:

            run_ffmpeg([
                "ffmpeg",
                "-y",

                "-f",
                "concat",

                "-safe",
                "0",

                "-i",
                list_file,

                "-c",
                "copy",

                "-movflags",
                "+faststart",

                output_file
            ])

        except Exception:

            # ------------------------------------------------
            # Fallback: re-encode
            # ------------------------------------------------

            logger.warning(
                "Fast concat failed. Starting re-encode fallback."
            )

            if os.path.exists(output_file):
                os.remove(output_file)

            run_ffmpeg([
                "ffmpeg",
                "-y",

                "-f",
                "concat",

                "-safe",
                "0",

                "-i",
                list_file,

                "-c:v",
                "libx264",

                "-preset",
                "veryfast",

                "-crf",
                "23",

                "-pix_fmt",
                "yuv420p",

                "-c:a",
                "aac",

                "-b:a",
                "192k",

                "-movflags",
                "+faststart",

                output_file
            ])

        # ----------------------------------------------------
        # Return final video
        # ----------------------------------------------------

        with open(output_file, "rb") as file:

            final_video_data = file.read()

        logger.info(
            f"Concat completed successfully: {job_id}"
        )

        return send_file(
            io.BytesIO(final_video_data),
            mimetype="video/mp4",
            as_attachment=True,
            download_name="final_video.mp4"
        )

    except Exception as e:

        logger.exception(
            f"Concat failed: {job_id}"
        )

        return jsonify({
            "success": False,
            "job_id": job_id,
            "error": str(e)
        }), 500

    finally:

        cleanup_files(temp_files)


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(404)
def not_found(error):

    return jsonify({
        "success": False,
        "error": "Endpoint not found"
    }), 404


@app.errorhandler(405)
def method_not_allowed(error):

    return jsonify({
        "success": False,
        "error": "Method not allowed"
    }), 405


@app.errorhandler(413)
def request_too_large(error):

    return jsonify({
        "success": False,
        "error": "Request too large"
    }), 413


@app.errorhandler(Exception)
def global_error(error):

    logger.exception(
        "Unhandled server error"
    )

    return jsonify({
        "success": False,
        "error": str(error)
    }), 500


# ============================================================
# RENDER START
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    logger.info(
        f"Starting server on port {port}"
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
```



