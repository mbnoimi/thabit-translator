#!/usr/bin/env python3
"""
core/speech_to_text.py - CPU-only speech-to-text using Vosk
Generates SRT subtitles from video/audio speech directly.
Designed for legacy hardware: lightweight, offline, no GPU required.
"""
import os
import sys
import json
import wave
import subprocess
import shutil
import tempfile
import time
import re
from pathlib import Path
from typing import Optional, List, Tuple

# Ensure CPU mode
os.environ["ARGOS_DEVICE_TYPE"] = "cpu"
os.environ["CUDA_VISIBLE_DEVICES"] = ""

try:
    from vosk import Model, KaldiRecognizer, SetLogLevel
except ImportError:
    Model = KaldiRecognizer = SetLogLevel = None

# Constants
DEFAULT_MODEL_NAME = "vosk-model-small-en-us-0.15"
SUPPORTED_LANGUAGES = {
    "en": "vosk-model-small-en-us-0.15", "en_US": "vosk-model-small-en-us-0.15", "en-US": "vosk-model-small-en-us-0.15",
    "en_GB": "vosk-model-small-en-us-0.15", "en-GB": "vosk-model-small-en-us-0.15", "en_UK": "vosk-model-small-en-us-0.15", "en-UK": "vosk-model-small-en-us-0.15",
    "en_IN": "vosk-model-small-en-in-0.4", "en-IN": "vosk-model-small-en-in-0.4",
    "ar": "vosk-model-small-ar-0.22", "ar_SA": "vosk-model-small-ar-0.22",
    "fr": "vosk-model-small-fr-0.22", "fr_FR": "vosk-model-small-fr-0.22",
    "de": "vosk-model-small-de-0.15", "de_DE": "vosk-model-small-de-0.15",
    "es": "vosk-model-small-es-0.22", "es_ES": "vosk-model-small-es-0.22",
    "ru": "vosk-model-small-ru-0.4", "ru_RU": "vosk-model-small-ru-0.4",
    "pt": "vosk-model-small-pt-0.3", "pt_BR": "vosk-model-small-pt-0.3",
    "tr": "vosk-model-small-tr-0.3", "tr_TR": "vosk-model-small-tr-0.3",
    "zh": "vosk-model-small-cn-0.3", "zh_CN": "vosk-model-small-cn-0.3",
    "ja": "vosk-model-small-ja-0.22", "ja_JP": "vosk-model-small-ja-0.22",
    "ko": "vosk-model-small-ko-0.22", "ko_KR": "vosk-model-small-ko-0.22",
    "it": "vosk-model-small-it-0.4", "it_IT": "vosk-model-small-it-0.4",
    "nl": "vosk-model-small-nl-0.22", "nl_NL": "vosk-model-small-nl-0.22",
    "pl": "vosk-model-small-pl-0.4", "pl_PL": "vosk-model-small-pl-0.4",
    "uk": "vosk-model-small-uk-0.4", "uk_UA": "vosk-model-small-uk-0.4",
}
VOSK_SAMPLE_RATE = 16000
CHUNK_SIZE = 4000


def _get_cache_dir(config: Optional[dict] = None) -> Path:
    """Get cache directory matching argostranslate pattern with robust ~ expansion."""
    base_cache = Path.home() / ".cache" / "thabit_translator"
    if config and "settings" in config and "cache_dir" in config["settings"]:
        raw = str(config["settings"]["cache_dir"]).strip().strip('"').strip("'")
        base_cache = Path(os.path.expanduser(raw))
    return base_cache / "vosk_models"


def _is_valid_vosk_model(model_dir: Path) -> bool:
    """Return True if model_dir contains a valid Vosk model (conf/model.conf)."""
    return (model_dir / "conf" / "model.conf").exists()


def _download_model(model_name: str, dest_path: Path) -> None:
    """Download and extract Vosk model, handling nested ZIP folders correctly."""
    base_url = "https://alphacephei.com/vosk/models"
    model_url = f"{base_url}/{model_name}.zip"
    print(f"[INFO] Downloading Vosk model: {model_name}")
    print(f"       Destination: {dest_path}")

    import requests
    import zipfile

    temp_dir = Path(tempfile.mkdtemp(prefix="thabit_vosk_"))
    zip_path = temp_dir / f"{model_name}.zip"

    try:
        # Download
        with requests.get(model_url, stream=True, timeout=300) as r:
            r.raise_for_status()
            total = int(r.headers.get('content-length', 0))
            downloaded = 0
            with open(zip_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            print(f"\r[INFO] Downloading: {min(100, int(100 * downloaded / total))}% ", end='', flush=True)
        print(f"\r[INFO] Download complete. Extracting...")

        # Extract
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)

        # Find the actual model root (look for conf/model.conf up to 2 levels deep)
        model_src = None
        for root, dirs, files in os.walk(temp_dir):
            if "conf" in dirs and (Path(root) / "conf" / "model.conf").exists():
                model_src = Path(root)
                break

        if model_src is None:
            # Fallback: maybe the model folder is the first extracted directory
            extracted_dirs = [d for d in temp_dir.iterdir() if d.is_dir()]
            if extracted_dirs:
                model_src = extracted_dirs[0]
                if not _is_valid_vosk_model(model_src):
                    # Check inside that directory
                    inner_dirs = [d for d in model_src.iterdir() if d.is_dir()]
                    if inner_dirs and _is_valid_vosk_model(inner_dirs[0]):
                        model_src = inner_dirs[0]
                    else:
                        raise FileNotFoundError("Extracted ZIP does not contain a valid Vosk model structure")
            else:
                raise FileNotFoundError("ZIP file is empty or corrupted")

        if dest_path.exists():
            shutil.rmtree(dest_path)
        shutil.move(str(model_src), str(dest_path))
        print(f"[INFO] Model ready: {dest_path}")

    except Exception as e:
        print(f"\n[ERROR] Failed to download/extract model: {e}")
        print(f"[HINT] Manually download from {model_url} and extract to {dest_path}")
        # Clean up partial dest_path if created
        if dest_path.exists():
            shutil.rmtree(dest_path, ignore_errors=True)
        raise
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


def _get_model_path(lang: str, model_name: Optional[str] = None, config: Optional[dict] = None) -> Path:
    """Resolve model path, download if needed. Returns absolute path to a valid model."""
    lang_key = lang.strip()
    if model_name is None:
        model_name = SUPPORTED_LANGUAGES.get(lang_key)
        if model_name is None:
            base_lang = lang_key.split("_")[0].split("-")[0]
            model_name = SUPPORTED_LANGUAGES.get(base_lang, DEFAULT_MODEL_NAME)
            print(f"[INFO] Locale '{lang_key}' mapped to base model: {model_name}")

    cache_dir = _get_cache_dir(config)
    model_path = (cache_dir / model_name).resolve()

    # If model exists but is invalid, delete it and re-download
    if model_path.exists() and not _is_valid_vosk_model(model_path):
        print(f"[WARN] Existing model at {model_path} is corrupt/incomplete. Re-downloading...")
        shutil.rmtree(model_path)

    if not model_path.exists():
        _download_model(model_name, model_path)

    # Final validation
    if not _is_valid_vosk_model(model_path):
        raise RuntimeError(f"Failed to obtain valid Vosk model at {model_path}")
    return model_path


def _extract_audio(video_path: str, temp_wav: Path, sample_rate: int = VOSK_SAMPLE_RATE) -> bool:
    """Extract audio from video to WAV using ffmpeg."""
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        ffmpeg_path = get_ffmpeg_exe()
    except ImportError:
        ffmpeg_path = "ffmpeg"

    cmd = [
        ffmpeg_path, "-i", video_path,
        "-vn", "-acodec", "pcm_s16le",
        "-ar", str(sample_rate), "-ac", "1",
        "-y", str(temp_wav)
    ]

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=3600)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Audio extraction failed: {e.stderr}")
        return False
    except FileNotFoundError:
        print("[ERROR] ffmpeg not found. Ensure imageio-ffmpeg is installed.")
        return False


def _format_timestamp(seconds: float) -> str:
    """Convert seconds to SRT timestamp format: HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def generate_srt_from_speech(
    video_path: str,
    output_srt: str,
    lang: str = "en",
    model_name: Optional[str] = None,
    confidence_threshold: float = 0.0,
    max_duration_sec: float = 7.0,
    max_chars: int = 80,
    split_on_pause: float = 0.8
) -> bool:
    """Generate SRT subtitles from video speech using Vosk ASR."""
    if SetLogLevel:
        SetLogLevel(-1)

    config = None
    try:
        from core.config import load_config
        config = load_config(None)
    except Exception:
        pass

    model_path = _get_model_path(lang, model_name, config)

    print(f"[INFO] Loading Vosk model: {model_path.name}")
    model = Model(str(model_path))

    cache_dir = _get_cache_dir(config)
    cache_dir.mkdir(parents=True, exist_ok=True)

    temp_wav = None
    try:
        fd, temp_path = tempfile.mkstemp(suffix=".wav", prefix="thabit_stt_", dir=cache_dir)
        os.close(fd)
        temp_wav = Path(temp_path)
    except Exception:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            temp_wav = Path(tmp.name)

    try:
        if not _extract_audio(video_path, temp_wav):
            return False

        with wave.open(str(temp_wav), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                print("[ERROR] Extracted audio must be mono 16-bit PCM")
                return False
            sample_rate = wf.getframerate()

        recognizer = KaldiRecognizer(model, sample_rate)
        recognizer.SetWords(True)

        print("[INFO] Processing audio for speech recognition...")
        results: List[dict] = []

        with wave.open(str(temp_wav), "rb") as wf:
            while True:
                data = wf.readframes(CHUNK_SIZE)
                if len(data) == 0:
                    break
                if recognizer.AcceptWaveform(data):
                    result = json.loads(recognizer.Result())
                    if result.get("text"):
                        results.append(result)
            final = json.loads(recognizer.FinalResult())
            if final.get("text"):
                results.append(final)

        # Build a flat list of words with timestamps
        all_words = []
        for segment in results:
            if "result" in segment and isinstance(segment["result"], list):
                all_words.extend(segment["result"])
            elif "text" in segment and segment["text"]:
                # Fallback: approximate timing
                all_words.append({
                    "word": segment["text"],
                    "start": segment.get("start", 0),
                    "end": segment.get("end", 0)
                })

        if not all_words:
            print("[WARN] No words recognized.")
            return False

        # Split words into subtitle blocks using intelligent rules
        srt_lines = []
        index = 1
        current_block = []
        current_start = None
        current_text = ""

        def commit_block():
            nonlocal index, current_block, current_start, current_text, srt_lines
            if not current_block:
                return
            start = current_block[0]["start"]
            end = current_block[-1]["end"]
            text = " ".join(w["word"] for w in current_block).strip()
            # Remove extra spaces around punctuation
            text = re.sub(r'\s+([.,!?;:])', r'\1', text)
            if text:
                srt_lines.append(f"{index}")
                srt_lines.append(f"{_format_timestamp(start)} --> {_format_timestamp(end)}")
                srt_lines.append(text)
                srt_lines.append("")
                index += 1
            current_block = []
            current_start = None
            current_text = ""

        sentence_endings = re.compile(r'[.!?]+$')

        for i, w in enumerate(all_words):
            word_text = w.get("word", "")
            if not word_text:
                continue

            # If starting a new block
            if not current_block:
                current_start = w["start"]
                current_block = [w]
                current_text = word_text
                continue

            last_word = current_block[-1]
            gap = w["start"] - last_word["end"]
            current_duration = w["end"] - current_start
            tentative_text = current_text + " " + word_text

            # Conditions to split:
            # 1. Gap exceeds pause threshold
            # 2. Block duration exceeds max_duration
            # 3. Character count exceeds max_chars
            # 4. Previous word ends with sentence-ending punctuation
            split = False
            if gap > split_on_pause:
                split = True
            elif current_duration > max_duration_sec:
                split = True
            elif len(tentative_text) > max_chars:
                split = True
            elif sentence_endings.search(current_text):
                split = True

            if split:
                commit_block()
                current_block = [w]
                current_start = w["start"]
                current_text = word_text
            else:
                current_block.append(w)
                current_text = tentative_text

        commit_block()  # last block

        with open(output_srt, "w", encoding="utf-8") as f:
            f.write("\n".join(srt_lines))

        print(f"[SUCCESS] SRT saved: {output_srt} ({len(srt_lines)//4} subtitles)")
        return True

    except Exception as e:
        print(f"[ERROR] Speech-to-text failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        # GUARANTEED cleanup of temp WAV
        if temp_wav and temp_wav.exists():
            try:
                temp_wav.unlink(missing_ok=True)
            except Exception:
                pass

        # Safety cleanup: remove orphaned thabit_stt_*.wav files older than 1 hour
        try:
            now = time.time()
            for f in cache_dir.glob("thabit_stt_*.wav"):
                if now - f.stat().st_mtime > 3600:
                    f.unlink(missing_ok=True)
        except Exception:
            pass

        # Clear model from memory for low-RAM systems
        try:
            del model
        except Exception:
            pass


def list_available_models() -> List[Tuple[str, str, str]]:
    """Return list of (lang_code, model_name, estimated_size) for available models."""
    model_sizes = {
        "vosk-model-small-en-us-0.15": "~40MB", "vosk-model-small-en-in-0.4": "~45MB",
        "vosk-model-small-ar-0.22": "~60MB", "vosk-model-small-fr-0.22": "~55MB",
        "vosk-model-small-de-0.15": "~40MB", "vosk-model-small-es-0.22": "~55MB",
        "vosk-model-small-ru-0.4": "~70MB", "vosk-model-small-pt-0.3": "~60MB",
        "vosk-model-small-tr-0.3": "~55MB", "vosk-model-small-cn-0.3": "~80MB",
        "vosk-model-small-ja-0.22": "~55MB", "vosk-model-small-ko-0.22": "~55MB",
        "vosk-model-small-it-0.4": "~70MB", "vosk-model-small-nl-0.22": "~55MB",
        "vosk-model-small-pl-0.4": "~70MB", "vosk-model-small-uk-0.4": "~70MB",
    }
    return [(lang, model, model_sizes.get(model, "~50MB")) for lang, model in SUPPORTED_LANGUAGES.items()]