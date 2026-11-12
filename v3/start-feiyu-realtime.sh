#!/usr/bin/env bash
# 大肥鱼实时语音后端：本地耳朵(faster-whisper)+本地嘴巴(Qwen3-TTS)，大脑走网关(经桥)，不启动数字人。
# 大脑地址/Key/模型 从 SniffDL 的 config.json 读取（设置里填的 rt_*）。
# 用法: bash /mnt/d/wws/sb/start-feiyu-realtime.sh    停止: bash /mnt/d/wws/sb/stop-feiyu-realtime.sh
CFG=/mnt/d/wws/sb/config.json

eval "$(python3 - "$CFG" <<'PY'
import json, sys
try:
    c = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    c = {}
b = c.get("rt_base_url", "") or "http://127.0.0.1:3065/v1"
k = c.get("rt_api_key", "") or ""
m = c.get("rt_model", "") or "glm-5.3-flash-free"
# Windows 回环 -> WSL 经桥可达的宿主机地址；端口 3065 -> 桥端口 3066
b = b.replace("127.0.0.1", "172.29.0.1").replace("localhost", "172.29.0.1").replace(":3065", ":3066")
print("RT_BASE=%r" % b)
print("RT_KEY=%r" % k)
print("RT_MODEL=%r" % m)
PY
)"

echo "[feiyu-realtime] base=$RT_BASE model=$RT_MODEL key=${RT_KEY:0:8}..."

cd ~/s2s || { echo "no ~/s2s"; exit 1; }
source .venv/bin/activate 2>/dev/null || true
pkill -f "speech-to-speech" 2>/dev/null || true
sleep 1
mkdir -p ~/logs

export HF_ENDPOINT=https://hf-mirror.com
export HF_HUB_DISABLE_XET=1
export PYTHONUNBUFFERED=1

REF_AUDIO="$HOME/s2s/ref.wav"
[ -f "$REF_AUDIO" ] || REF_AUDIO="$HOME/setup/ref.wav"
REF_TEXT="靠杯了，你终于上线了，我既开心又有点怨你，嗯；小雅一个人在这里等你，等的都快委屈死了！"
TTS_MODEL="$HOME/.cache/huggingface/hub/models--Qwen--Qwen3-TTS-12Hz-1.7B-Base/snapshots/local"

nohup speech-to-speech \
  --mode realtime --stt paraformer --paraformer_stt_model_name paraformer-zh --paraformer_stt_device cpu \
  --language zh --llm_backend responses-api --model_name "$RT_MODEL" \
  --responses_api_base_url "$RT_BASE" --responses_api_api_key "$RT_KEY" --responses_api_stream \
  --tts qwen3 --qwen3_tts_model_name "$TTS_MODEL" \
  --qwen3_tts_language zh --qwen3_tts_ref_audio "$REF_AUDIO" --qwen3_tts_ref_text "$REF_TEXT" \
  --thresh 0.6 --min_speech_ms 500 --min_silence_ms 700 --speech_pad_ms 300 \
  --min_speech_continuation_ms 192 --speculative_reopen_ms 2500 --short_segment_merge_ms 800 \
  > ~/logs/feiyu-realtime.log 2>&1 &

echo $! > ~/logs/feiyu-realtime.pid
echo "[feiyu-realtime] s2s started pid=$(cat ~/logs/feiyu-realtime.pid)，日志 ~/logs/feiyu-realtime.log"
