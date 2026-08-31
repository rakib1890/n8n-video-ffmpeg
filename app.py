from flask import Flask, request, jsonify, send_file
import subprocess
import requests
import os
import uuid

app = Flask(__name__)


@app.route("/", methods=["GET"])
def home():
    return "FFmpeg Video + Audio Merge Service is running!"


@app.route("/merge", methods=["POST"])
def merge():
    data = request.get_json()
    video_url = data.get("video_url")
    audio_url = data.get("audio_url")

    if not video_url or not audio_url:
        return jsonify({
            "error": "video_url and audio_url are required"
        }), 400

    job_id = str(uuid.uuid4())
    video_file = f"/tmp/{job_id}_video.mp4"
    audio_file = f"/tmp/{job_id}_audio.mp3"
    output_file = f"/tmp/{job_id}_final.mp4"

    try:
        video_response = requests.get(video_url)
        video_response.raise_for_status()
        with open(video_file, "wb") as f:
            f.write(video_response.content)

        audio_response = requests.get(audio_url)
        audio_response.raise_for_status()
        with open(audio_file, "wb") as f:
            f.write(audio_response.content)

        subprocess.run([
            "ffmpeg",
            "-y",
            "-i", video_file,
            "-i", audio_file,
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            output_file
        ], check=True)

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
            if os.path.exists(file):
                os.remove(file)


@app.route("/concat", methods=["POST"])
def concat():
    data = request.get_json()
    video_urls = data.get("video_urls")

    if not video_urls or not isinstance(video_urls, list) or len(video_urls) == 0:
        return jsonify({
            "error": "video_urls must be a non-empty list of video URLs, in order"
        }), 400

    job_id = str(uuid.uuid4())
    local_files = []
    list_file = f"/tmp/{job_id}_list.txt"
    output_file = f"/tmp/{job_id}_final.mp4"

    try:
        for i, url in enumerate(video_urls):
            local_path = f"/tmp/{job_id}_{i}.mp4"
            r = requests.get(url)
            r.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(r.content)
            local_files.append(local_path)

        with open(list_file, "w") as f:
            for path in local_files:
                f.write(f"file '{path}'\n")

        subprocess.run([
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-c", "copy",
            output_file
        ], check=True)

        return send_file(
            output_file,
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
        for f in local_files + [list_file, output_file]:
            if os.path.exists(f):
                os.remove(f)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
