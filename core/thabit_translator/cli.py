#!/usr/bin/env python3
"""
Thabit Translator - CLI (TUI)
"""
import argparse
import sys
import os
import shlex

from core.paths import resolve_input_path

class TermColors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    RED = "\033[91m"
    GRAY = "\033[90m"

def print_header():
    banner = f"""
{TermColors.CYAN}╔═══════════════════════════════════════════════════════════════╗
║      {TermColors.BOLD}Thabit Translator & Extractor{TermColors.RESET}{TermColors.CYAN}                     ║
║                 {TermColors.DIM}CPU-Optimized Edition{TermColors.RESET}{TermColors.CYAN}                      ║
╚═══════════════════════════════════════════════════════════════════════╝{TermColors.RESET}
"""
    print(banner)

def print_step(step_num, total, description, status=None):
    indent = "  "
    checkbox = f"{TermColors.BLUE}[{step_num}/{total}]{TermColors.RESET}"
    if status == "done":
        checkbox = f"{TermColors.GREEN}[✓]{TermColors.RESET}"
    elif status == "running":
        checkbox = f"{TermColors.YELLOW}[…]{TermColors.RESET}"
    elif status == "error":
        checkbox = f"{TermColors.RED}[✗]{TermColors.RESET}"
    
    if status == "running":
        print(f"{indent}{checkbox} {TermColors.YELLOW}{description}...{TermColors.RESET}")
    elif status:
        print(f"{indent}{checkbox} {description} {TermColors.DIM}({status}){TermColors.RESET}")
    else:
        print(f"{indent}{checkbox} {description}")

def print_path(path, label=None):
    if label:
        print(f"  {TermColors.GRAY}├─{TermColors.RESET} {label}: {TermColors.CYAN}{path}{TermColors.RESET}")
    else:
        print(f"  {TermColors.GRAY}├─{TermColors.RESET} {TermColors.CYAN}{path}{TermColors.RESET}")

def print_keyval(key, value, indent=2):
    print(f"{' '*indent}{TermColors.GRAY}{key}:{TermColors.RESET} {value}")

def print_success(message):
    print(f"{TermColors.GREEN}✓ {message}{TermColors.RESET}")

def print_error(message):
    print(f"{TermColors.RED}✗ {message}{TermColors.RESET}")

def print_warn(message):
    print(f"{TermColors.YELLOW}⚠ {message}{TermColors.RESET}")

def print_info(message):
    print(f"{TermColors.BLUE}ℹ {message}{TermColors.RESET}")

def print_sep():
    print(f"{TermColors.DIM}{'─' * 60}{TermColors.RESET}")

def route_translate(args):
    from core.translate import translate_srt
    args.input = resolve_input_path(args.input)
    print_step(1, 1, "Translating subtitles", "running")
    translate_srt(args.input, args.output, args.src, args.tgt)
    print_step(1, 1, "Translating subtitles", "done")
    print_path(args.output, "Output")

def route_extract(args):
    from core.config import load_config
    from core.extract import list_embedded_subs, extract_subtitles
    args.video = resolve_input_path(args.video)
    config = load_config(args.config)
    lang_hint = args.src or config["settings"].get("default_source_lang", "en")
    
    print_step(1, 1, "Extracting subtitles", "running")
    if args.list:
        print_info(f"Scanning: {args.video}")
        list_embedded_subs(args.video)
    else:
        result = extract_subtitles(args.video, args.stream, lang_hint)
        print_step(1, 1, "Extracting subtitles", "done")
        if result:
            print_path(result, "Extracted")
    print_keyval("Language hint", lang_hint)

def route_download(args):
    from providers import download_subtitle
    from core.config import load_config
    args.video = resolve_input_path(args.video)
    config = load_config(args.config)
    lang = args.tgt or config["opensubtitles"].get("default_language", "ar")
    provider_list = [args.provider.strip()] if args.provider else None
    
    print_step(1, 1, "Downloading subtitles", "running")
    print_keyval("Language", lang)
    if args.query:
        print_keyval("Query", args.query)
    
    result = download_subtitle(
        args.video,
        config,
        lang,
        args.query,
        not args.no_hash,
        provider_list,
        select_index=args.select,
        auto_select=args.auto_select,
    )
    print_step(1, 1, "Downloading subtitles", "done" if result else "error")
    if result:
        print_path(result, "Downloaded")

def route_stt(args):
    from core.speech_to_text import generate_srt_from_speech
    from core.translate import translate_srt
    from core.config import load_config
    args.video = resolve_input_path(args.video)
    config = load_config(args.config)
    lang = args.src or config["settings"].get("default_source_lang", "en")
    lang = lang.strip().strip('"').strip("'")

    base_name = args.video.rsplit(".", 1)[0]
    output_srt = f"{base_name}.{lang}.srt"

    print_step(1, 2, "Speech-to-Text", "running")
    print_keyval("Input", args.video)
    print_keyval("Language", lang)
    if args.model:
        print_keyval("Model", args.model)
    if args.max_duration:
        print_keyval("Max duration", f"{args.max_duration}s")
    if args.max_chars:
        print_keyval("Max chars", str(args.max_chars))

    success = generate_srt_from_speech(
        args.video, output_srt, lang=lang, model_name=args.model,
        max_duration_sec=args.max_duration, max_chars=args.max_chars,
        split_on_pause=args.split_pause
    )
    print_step(1, 2, "Speech-to-Text", "done" if success else "error")

    if success:
        print_path(output_srt, "Generated")

    if success and args.tgt:
        print_step(2, 2, f"Translating to {args.tgt}", "running")
        final_srt = f"{base_name}.{args.tgt}.srt"
        src_lang_clean = lang.split("_")[0].split("-")[0]
        translate_srt(output_srt, final_srt, src_lang_clean, args.tgt)
        print_step(2, 2, f"Translating to {args.tgt}", "done")
        print_path(final_srt, "Output")
    elif success:
        print_path(output_srt, "Output")

def route_auto(args):
    from core.auto_workflow import run_auto_mode
    from core.config import load_config
    args.input = resolve_input_path(args.input)
    config = load_config(args.config)

    print_step(1, 4, "Auto mode", "running")
    print_keyval("Input", args.input)
    if args.tgt:
        print_keyval("Target", args.tgt)
    if args.src:
        print_keyval("Source", args.src)

    result = run_auto_mode(
        args.input,
        config_path=args.config,
        target_lang=args.tgt,
        source_lang=args.src,
        verbose=True,
        force=getattr(args, "force", False),
        stt=getattr(args, "stt", "ask"),
    )

    if result:
        print_step(1, 4, "Auto mode", "done")
        print_path(result, "Output")
    else:
        print_step(1, 4, "Auto mode", "error")
        print_error("No subtitle produced")

def print_help_menu():
    print(f"""
{TermColors.BOLD}USAGE:{TermColors.RESET}
  {TermColors.CYAN}thabit-translator{TermColors.RESET} <command> [options]

{TermColors.BOLD}COMMANDS:{TermColors.RESET}
  {TermColors.GREEN}translate{TermColors.RESET}     Translate existing SRT files
  {TermColors.GREEN}extract{TermColors.RESET}     Extract embedded subtitles from video
  {TermColors.GREEN}download{TermColors.RESET}   Download subtitles from providers
  {TermColors.GREEN}stt{TermColors.RESET}        Speech-to-text (generate SRT from audio)
  {TermColors.GREEN}auto{TermColors.RESET}        Full automated workflow

{TermColors.BOLD}EXAMPLES:{TermColors.RESET}
  {TermColors.YELLOW}# Translate subtitles{TermColors.RESET}
  {TermColors.GRAY}translate input.srt output.srt -t ar{TermColors.RESET}

  {TermColors.YELLOW}# Extract embedded subtitles{TermColors.RESET}
  {TermColors.GRAY}extract movie.mp4 -l{TermColors.RESET}

  {TermColors.YELLOW}# Download subtitles{TermColors.RESET}
  {TermColors.GRAY}download movie.mp4 -t ar -q "Movie Title"{TermColors.RESET}

  {TermColors.YELLOW}# Full auto mode (recommended){TermColors.RESET}
  {TermColors.GRAY}auto movie.mp4 -t ar{TermColors.RESET}

{TermColors.BOLD}OPTIONS:{TermColors.RESET}
  {TermColors.CYAN}?{TermColors.RESET}           Show this help menu
  {TermColors.CYAN}q, quit{TermColors.RESET}      Exit the application

{TermColors.BOLD}GET HELP:{TermColors.RESET}
  {TermColors.CYAN}translate -h{TermColors.RESET}
  {TermColors.CYAN}extract -h{TermColors.RESET}
  {TermColors.CYAN}download -h{TermColors.RESET}
  {TermColors.CYAN}stt -h{TermColors.RESET}
  {TermColors.CYAN}auto -h{TermColors.RESET}
""")


def get_input_loop(parser):
    while True:
        try:
            user_input = input(f"\n{TermColors.CYAN}thabit>{TermColors.RESET} ").strip()
        except EOFError:
            print()
            break

        if not user_input:
            continue

        if user_input.lower() in ("q", "quit", "exit"):
            print(f"{TermColors.GRAY}Goodbye!{TermColors.RESET}")
            break

        if user_input == "?":
            print_header()
            print_sep()
            print_help_menu()
            print_sep()
            continue

        cmd_parts = shlex.split(user_input)
        sys.argv = ["thabit-translator"] + cmd_parts
        try:
            args = parser.parse_args()
        except SystemExit:
            continue

        if args.mode == "translate":
            route_translate(args)
        elif args.mode == "extract":
            route_extract(args)
        elif args.mode == "download":
            route_download(args)
        elif args.mode == "stt":
            route_stt(args)
        elif args.mode == "auto":
            route_auto(args)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Thabit Translator & Extractor (CPU-Only)",
        add_help=True,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    p_translate = subparsers.add_parser("translate", help="Translate an existing SRT file")
    p_translate.add_argument("input", help="Input SRT file path")
    p_translate.add_argument("output", help="Output SRT file path")
    p_translate.add_argument("-s", "--src", default="en", help="Source language code (default: en)")
    p_translate.add_argument("-t", "--tgt", default="ar", help="Target language code (default: ar)")

    p_extract = subparsers.add_parser("extract", help="List or extract embedded subtitles from video")
    p_extract.add_argument("video", help="Video file path")
    p_extract.add_argument("-l", "--list", action="store_true", help="List embedded subtitle streams")
    p_extract.add_argument("-s", "--stream", type=int, default=None, help="Stream index to extract (0-based)")
    p_extract.add_argument("--src", default=None, help="Source language hint for extraction")
    p_extract.add_argument("-c", "--config", default=None, help="Config file path")

    p_download = subparsers.add_parser("download", help="Download subtitles from online providers")
    p_download.add_argument("video", help="Video file path")
    p_download.add_argument("-t", "--tgt", default=None, help="Target language for download (falls back to config)")
    p_download.add_argument("-q", "--query", default=None, help="Search query for providers")
    p_download.add_argument("--no-hash", action="store_true", help="Skip file hash lookup")
    p_download.add_argument("-p", "--provider", default=None, help="Provider: opensubtitles, subdl, subsource")
    p_download.add_argument("--select", type=int, default=None, help="Select subtitle by index (0-based)")
    p_download.add_argument("--auto", dest="auto_select", action="store_true", default=True, help="Auto-select best subtitle (default)")
    p_download.add_argument("--no-auto", dest="auto_select", action="store_false", help="Disable auto-selection")
    p_download.add_argument("-c", "--config", default=None, help="Config file path")

    p_stt = subparsers.add_parser("stt", help="Generate subtitles from video speech (AI STT)")
    p_stt.add_argument("video", help="Video file path")
    p_stt.add_argument("-s", "--src", default=None, help="Source language for STT (default: en)")
    p_stt.add_argument("-m", "--model", default=None, help="Specific Vosk model name")
    p_stt.add_argument("-t", "--tgt", default=None, help="Target language for post-STT translation")
    p_stt.add_argument("--max-duration", type=float, default=7.0, help="Max subtitle duration in seconds (default: 7.0)")
    p_stt.add_argument("--max-chars", type=int, default=80, help="Max characters per subtitle line (default: 80)")
    p_stt.add_argument("--split-pause", type=float, default=0.8, help="Pause threshold in seconds to split subtitles (default: 0.8)")
    p_stt.add_argument("-c", "--config", default=None, help="Config file path")

    p_auto = subparsers.add_parser("auto", help="Auto mode: full workflow (video/subtitle/folder -> download/extract/translate/STT)")
    p_auto.add_argument("input", help="Input video file, subtitle file, or folder (folders are processed recursively)")
    p_auto.add_argument("-t", "--tgt", default=None, help="Target language (default: from config)")
    p_auto.add_argument("-s", "--src", default=None, help="Source language hint (default: from config)")
    p_auto.add_argument("-c", "--config", default=None, help="Config file path")
    p_auto.add_argument("--force", action="store_true", help="Folder mode: re-process files that already have a valid subtitle")
    p_auto.add_argument("--stt", choices=["ask", "yes", "no"], default="ask",
                        help="When nothing can produce a subtitle (providers blocked/empty, no embedded track): "
                             "ask (default, prompts on a terminal), yes (run STT), no (skip the video)")

    return parser


def main():
    if len(sys.argv) == 1:
        print_header()
        print_sep()
        print_help_menu()
        print_sep()
        parser = build_parser()
        get_input_loop(parser)
        return

    parser = build_parser()
    print_header()
    print_sep()

    args = parser.parse_args()

    if args.mode == "translate":
        route_translate(args)
    elif args.mode == "extract":
        route_extract(args)
    elif args.mode == "download":
        route_download(args)
    elif args.mode == "stt":
        route_stt(args)
    elif args.mode == "auto":
        route_auto(args)

    print_sep()


if __name__ == "__main__":
    main()