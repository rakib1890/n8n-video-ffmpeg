from flask import Flask, request, jsonify, send_file
import subprocess
import requests
import os
import uuid
import tempfile
import shutil

app = Flask(__name__)

DOWNLOAD_TIMEOUT = 120
FFMPEG_TIMEOUT = 600


@app.route("/", methods=["GET"])
def home():
    return "FFmpeg Video + Audio Merge + Concat Service is running!"


def download_file(url, path):
    if not url or not isinstance(url, str):
        raise ValueError("Invalid file URL")

    response = requests.get(
        url,
        stream=True,
        timeout=DOWNLOAD_TIMEOUT
    )
    response.raise_for_status()

    with open(path, "wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)


def run_ffmpeg(command):
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=FFMPEG_TIMEOUT
    )

    if result.returncode != 0:
        raise RuntimeError(
            "FFmpeg failed:\n" + result.stderr[-5000:]
        )


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

    job_id = str(uuid.uuid4())

    video_file = f"/tmp/{job_id}_video.mp4"
    audio_file = f"/tmp/{job_id}_audio.mp3"
    output_file = f"/tmp/{job_id}_merged.mp4"

    try:
        download_file(video_url, video_file)
        download_file(audio_url, audio_file)

        run_ffmpeg([
            "ffmpeg",
            "-y",
            "-i", video_file,
            "-i", audio_file,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            output_file
        ])

        return send_file(
            output_file,
            mimetype="video/mp4",
            as_attachment=True,
            download_name="segment_merged.mp4"
        )

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

    finally:
        for file in [video_file, audio_file, output_file]:
            try:
                if os.path.exists(file):
                    os.remove(file)
            except Exception:
                pass


@app.route("/concat", methods=["POST"])
def concat():
    data = request.get_json(silent=True) or {}

    segments = data.get("segments")

    # New format:
    # {
    #   "segments": [
    #       {
    #           "video_url": "...",
    #           "audio_url": "..."
    #       }
    #   ]
    # }

    if segments is None:
        # Backward compatibility with old format
        video_urls = data.get("video_urls")

        if not isinstance(video_urls, list) or not video_urls:
            return jsonify({
                "success": False,
                "error": "segments or video_urls is required"
            }), 400

        segments = [
            {
                "video_url": url,
                "audio_url": None
            }
            for url in video_urls
        ]

    if not isinstance(segments, list) or len(segments) == 0:
        return jsonify({
            "success": False,
            "error": "segments must be a non-empty list"
        }), 400

    job_id = str(uuid.uuid4())
    work_dir = f"/tmp/{job_id}"

    os.makedirs(work_dir, exist_ok=True)

    merged_files = []
    list_file = os.path.join(work_dir, "concat_list.txt")
    final_file = os.path.join(work_dir, "final_video.mp4")

    try:
        # --------------------------------------------------
        # STEP 1: Download + merge every video/audio pair
        # --------------------------------------------------

        for index, segment in enumerate(segments):

            if isinstance(segment, str):
                video_url = segment
                audio_url = None
            else:
                video_url = segment.get("video_url")
                audio_url = segment.get("audio_url")

            if not video_url:
                raise ValueError(
                    f"Segment {index + 1}: video_url is missing"
                )

            video_file = os.path.join(
                work_dir,
                f"video_{index}.mp4"
            )

            download_file(video_url, video_file)

            # If audio exists, merge it with the video
            if audio_url:

                audio_file = os.path.join(
                    work_dir,
                    f"audio_{index}.mp3"
                )

                merged_file = os.path.join(
                    work_dir,
                    f"merged_{index}.mp4"
                )

                download_file(audio_url, audio_file)

                run_ffmpeg([
                    "ffmpeg",
                    "-y",
                    "-i", video_file,
                    "-i", audio_file,
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "23",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-shortest",
                    "-movflags", "+faststart",
                    merged_file
                ])

                merged_files.append(merged_file)

            else:
                # No audio supplied, keep video
                merged_files.append(video_file)

        # --------------------------------------------------
        # STEP 2: Create FFmpeg concat list
        # --------------------------------------------------

        with open(list_file, "w", encoding="utf-8") as f:
            for file_path in merged_files:
                safe_path = file_path.replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")

        # --------------------------------------------------
        # STEP 3: Concatenate all segments
        # Re-encode for maximum compatibility
        # --------------------------------------------------

        run_ffmpeg([
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            final_file
        ])

        return send_file(
            final_file,
            mimetype="video/mp4",
            as_attachment=True,
            download_name="final_video.mp4"
        )

    except Exception as e:

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "ffmpeg-video-audio-concat"
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )



