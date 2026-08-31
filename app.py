from flask import Flask, request, jsonify, send_file
import subprocess
import requests
import os
import uuid
import io
import logging
import time

app = Flask(__name__)

# ============================================================
# CONFIGURATION
# ============================================================

PORT = int(os.environ.get("PORT", "10000"))

MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB
DOWNLOAD_TIMEOUT = (20, 300)
DOWNLOAD_RETRIES = 3

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# COMMON HELPERS
# ============================================================

def is_valid_url(url):
    return (
        isinstance(url, str)
        and (
            url.startswith("http://")
            or url.startswith("https://")
        )
    )


def download_file(url, output_path):
    last_error = None

    for attempt in range(1, DOWNLOAD_RETRIES + 1):

        try:
            logger.info(
                "Downloading file: attempt %s/%s",
                attempt,
                DOWNLOAD_RETRIES
            )

            with requests.get(
                url,
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
                headers={
                    "User-Agent": "Mozilla/5.0 FFmpeg-Service"
                }
            ) as response:

                response.raise_for_status()

                content_length = response.headers.get(
                    "content-length"
                )

                if content_length:
                    if int(content_length) > MAX_FILE_SIZE:
                        raise ValueError(
                            "File is larger than 500 MB"
                        )

                downloaded = 0

                with open(output_path, "wb") as file:

                    for chunk in response.iter_content(
                        chunk_size=1024 * 1024
                    ):

                        if not chunk:
                            continue

                        downloaded += len(chunk)

                        if downloaded > MAX_FILE_SIZE:
                            raise ValueError(
                                "Downloaded file exceeded 500 MB"
                            )

                        file.write(chunk)

            logger.info(
                "Download completed: %.2f MB",
                downloaded / 1024 / 1024
            )

            return True

        except Exception as error:

            last_error = error

            logger.warning(
                "Download failed: %s",
                error
            )

            if os.path.exists(output_path):
                os.remove(output_path)

            if attempt < DOWNLOAD_RETRIES:
                time.sleep(2)

    raise RuntimeError(
        f"Download failed after {DOWNLOAD_RETRIES} attempts: "
        f"{last_error}"
    )


def run_ffmpeg(command):
    logger.info("Running FFmpeg")

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:

        logger.error(
            "FFmpeg error:\n%s",
            result.stderr[-5000:]
        )

        raise RuntimeError(
            result.stderr[-3000:]
        )

    return result


def cleanup(files):

    for file in files:

        try:

            if os.path.exists(file):
                os.remove(file)

        except Exception as error:

            logger.warning(
                "Cleanup failed for %s: %s",
                file,
                error
            )


def return_video(file_path, filename):

    with open(file_path, "rb") as file:
        data = file.read()

    return send_file(
        io.BytesIO(data),
        mimetype="video/mp4",
        as_attachment=True,
        download_name=filename
    )


# ============================================================
# HOME / HEALTH
# ============================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "success": True,
        "service": "FFmpeg Video + Audio Merge Service",
        "status": "running",
        "version": "3.0"
    })


@app.route("/health", methods=["GET"])
def health():

    return jsonify({
        "success": True,
        "status": "healthy"
    })


# ============================================================
# MERGE VIDEO + AUDIO
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

    if not is_valid_url(video_url):

        return jsonify({
            "success": False,
            "error": "Invalid video_url"
        }), 400

    if not is_valid_url(audio_url):

        return jsonify({
            "success": False,
            "error": "Invalid audio_url"
        }), 400

    job_id = str(uuid.uuid4())

    video_file = f"/tmp/{job_id}_video.mp4"
    audio_file = f"/tmp/{job_id}_audio.mp3"
    output_file = f"/tmp/{job_id}_merged.mp4"

    files = [
        video_file,
        audio_file,
        output_file
    ]

    logger.info(
        "Starting merge job: %s",
        job_id
    )

    try:

        # Download video
        download_file(
            video_url,
            video_file
        )

        # Download audio
        download_file(
            audio_url,
            audio_file
        )

        # ----------------------------------------------------
        # FAST MODE
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

        except Exception as fast_error:

            logger.warning(
                "Fast merge failed. Using fallback encoder."
            )

            if os.path.exists(output_file):
                os.remove(output_file)

            # ------------------------------------------------
            # FALLBACK MODE
            # ------------------------------------------------

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

        logger.info(
            "Merge successful: %s",
            job_id
        )

        return return_video(
            output_file,
            "segment_merged.mp4"
        )

    except Exception as error:

        logger.exception(
            "Merge failed: %s",
            job_id
        )

        return jsonify({
            "success": False,
            "job_id": job_id,
            "error": str(error)
        }), 500

    finally:

        cleanup(files)


# ============================================================
# CONCAT MULTIPLE VIDEOS
# ============================================================

@app.route("/concat", methods=["POST"])
def concat():

    data = request.get_json(silent=True) or {}

    # Supports both:
    #
    # {
    #   "videos": [...]
    # }
    #
    # and:
    #
    # {
    #   "video_urls": [...]
    # }

    video_urls = (
        data.get("videos")
        or data.get("video_urls")
    )

    if not video_urls:

        return jsonify({
            "success": False,
            "error": "videos or video_urls is required"
        }), 400

    if not isinstance(video_urls, list):

        return jsonify({
            "success": False,
            "error": "videos must be a list"
        }), 400

    if len(video_urls) == 0:

        return jsonify({
            "success": False,
            "error": "No videos provided"
        }), 400

    if len(video_urls) > 50:

        return jsonify({
            "success": False,
            "error": "Maximum 50 videos per job"
        }), 400

    # Validate all URLs

    for index, url in enumerate(video_urls):

        if not is_valid_url(url):

            return jsonify({
                "success": False,
                "error": (
                    f"Invalid video URL at index {index}"
                )
            }), 400

    job_id = str(uuid.uuid4())

    list_file = f"/tmp/{job_id}_concat.txt"
    output_file = f"/tmp/{job_id}_final.mp4"

    local_files = []

    logger.info(
        "Starting concat job: %s",
        job_id
    )

    try:

        # ----------------------------------------------------
        # DOWNLOAD VIDEOS IN ORDER
        # ----------------------------------------------------

        for index, url in enumerate(video_urls):

            local_file = (
                f"/tmp/{job_id}_segment_{index}.mp4"
            )

            download_file(
                url,
                local_file
            )

            local_files.append(local_file)

        # ----------------------------------------------------
        # CREATE FFmpeg CONCAT LIST
        # ----------------------------------------------------

        with open(
            list_file,
            "w",
            encoding="utf-8"
        ) as file:

            for video in local_files:

                file.write(
                    f"file '{video}'\n"
                )

        # ----------------------------------------------------
        # FAST CONCAT
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

            logger.warning(
                "Fast concat failed. "
                "Using re-encode fallback."
            )

            if os.path.exists(output_file):
                os.remove(output_file)

            # ------------------------------------------------
            # FALLBACK CONCAT
            # ------------------------------------------------

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

        logger.info(
            "Concat successful: %s",
            job_id
        )

        return return_video(
            output_file,
            "final_video.mp4"
        )

    except Exception as error:

        logger.exception(
            "Concat failed: %s",
            job_id
        )

        return jsonify({
            "success": False,
            "job_id": job_id,
            "error": str(error)
        }), 500

    finally:

        cleanup(
            local_files
            + [list_file, output_file]
        )


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


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    logger.info(
        "Starting FFmpeg service on port %s",
        PORT
    )

    app.run(
        host="0.0.0.0",
        port=PORT
    )



