#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
demo_dir="${repo_root}/workspace/demo"
source_video="${demo_dir}/semantic-video-demo-source.mp4"

for command_name in ffmpeg ffprobe; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Missing dependency: ${command_name}. Install FFmpeg first." >&2
    exit 1
  fi
done

mkdir -p "${demo_dir}"
if [[ ! -s "${source_video}" ]]; then
  echo "Generating a synthetic, copyright-free motion sample..."
  ffmpeg -y -v error \
    -f lavfi -i 'color=c=0x101820:s=640x360:r=30:d=4' \
    -f lavfi -i 'testsrc2=s=640x360:r=30:d=4' \
    -f lavfi -i 'color=c=0x203050:s=640x360:r=30:d=4,drawbox=x=mod(t*180\,480):y=100:w=160:h=120:color=orange:t=fill' \
    -f lavfi -i 'sine=frequency=220:sample_rate=48000:duration=12' \
    -filter_complex '[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p[v]' \
    -map '[v]' -map 3:a -t 12 \
    -c:v libx264 -preset veryfast -crf 20 \
    -c:a aac -b:a 128k -movflags +faststart \
    "${source_video}"
fi

duration="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "${source_video}")"
echo "Demo source ready: ${source_video} (${duration}s)"

if [[ "${1:-}" == "--generate-only" ]]; then
  exit 0
fi

if [[ -x "${repo_root}/.venv/bin/axonflow" ]]; then
  axonflow_command=("${repo_root}/.venv/bin/axonflow")
elif command -v axonflow >/dev/null 2>&1; then
  axonflow_command=("$(command -v axonflow)")
else
  echo 'AxonFlow is not installed. Run: pip install -e ".[dev]"' >&2
  exit 1
fi

demo_input="$(printf '{"source":"%s","description":"选择运动最强、画面变化最明显的精彩片段，排除静态开场","target_duration_seconds":4,"hard_subtitles":false}' "${source_video}")"

cd "${repo_root}"
echo "Running the built-in semantic-video-edit workflow..."
"${axonflow_command[@]}" run semantic-video-edit --input "${demo_input}"
echo "Finished. The registered output path is printed above; media is under: ${repo_root}/workspace/media/"
