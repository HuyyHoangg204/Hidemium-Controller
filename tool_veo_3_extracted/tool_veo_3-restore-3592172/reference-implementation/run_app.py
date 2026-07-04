import argparse
import logging
from pathlib import Path

from scheduler import BananaJob, BananaScheduler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Reference Banana Python app')
    parser.add_argument('--mode', choices=['image', 'video'])
    parser.add_argument('--tokens-file', help='Path to a text file containing one token per line')
    parser.add_argument('--prompts-file', help='Path to a text file containing prompts separated by blank lines')
    parser.add_argument('--aspect-ratio', default='16:9', choices=['16:9', '9:16', '1:1'])
    parser.add_argument('--model', choices=['GEM_PIX_2', 'NARWHAL'])
    parser.add_argument('--reference', help='Optional reference image path used for every prompt')
    parser.add_argument('--output-dir', help='Optional directory for saving outputs')
    parser.add_argument('--thread-count', type=int, help='Maximum number of concurrent Banana jobs')
    return parser


def ask_mode_if_missing(value: str | None) -> str:
    if value in {'image', 'video'}:
        return value
    while True:
        entered = input('Mode [image/video] (default image): ').strip().lower()
        if not entered:
            return 'image'
        if entered in {'image', 'video'}:
            return entered
        print('Invalid mode. Use image or video.')


def ask_model_if_missing(value: str | None) -> str:
    if value in {'GEM_PIX_2', 'NARWHAL'}:
        return value

    while True:
        entered = input('Model [GEM_PIX_2/NARWHAL] (default GEM_PIX_2): ').strip().upper()
        if not entered:
            return 'GEM_PIX_2'
        if entered in {'GEM_PIX_2', 'NARWHAL'}:
            return entered
        print('Invalid model. Use GEM_PIX_2 or NARWHAL.')


def ask_thread_count_if_missing(value: int | None) -> int:
    if isinstance(value, int) and value > 0:
        return value

    while True:
        entered = input('Thread count (default 1): ').strip()
        if not entered:
            return 1
        try:
            parsed = int(entered)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
        print('Invalid thread count. Use an integer greater than 0.')


def ask_reference_if_needed(mode: str, value: str | None) -> str | None:
    if value:
        return value
    if mode != 'video':
        return None
    while True:
        entered = input('Reference image path for video mode: ').strip().strip('"')
        if entered:
            return entered
        print('Video mode requires one reference image path.')


def read_tokens_from_file(path: str) -> list[str]:
    content = Path(path).read_text(encoding='utf-8')
    return [line.strip() for line in content.splitlines() if line.strip()]


def read_prompts_from_file(path: str) -> list[str]:
    content = Path(path).read_text(encoding='utf-8').strip()
    if not content:
        return []
    return [block.strip() for block in content.split('\n\n') if block.strip()]


def ask_tokens_interactive() -> list[str]:
    print('Paste tokens, one token per line. Press Enter on an empty line to finish.')
    tokens = []
    while True:
        line = input().strip()
        if not line:
            break
        tokens.append(line)
    if not tokens:
        raise SystemExit('At least one token is required')
    return tokens


def ask_prompts_interactive() -> list[str]:
    print('Paste prompts. Separate prompts with a blank line. Type END on its own line to finish.')
    lines: list[str] = []
    while True:
        line = input()
        if line.strip() == 'END':
            break
        lines.append(line)
    content = '\n'.join(lines).strip()
    prompts = [block.strip() for block in content.split('\n\n') if block.strip()]
    if not prompts:
        raise SystemExit('At least one prompt is required')
    return prompts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    args = build_parser().parse_args()
    mode = ask_mode_if_missing(args.mode)
    model = ask_model_if_missing(args.model) if mode == 'image' else 'VIDEO_I2V'
    thread_count = ask_thread_count_if_missing(args.thread_count)
    reference_path = ask_reference_if_needed(mode, args.reference)
    tokens = read_tokens_from_file(args.tokens_file) if args.tokens_file else ask_tokens_interactive()
    prompts = read_prompts_from_file(args.prompts_file) if args.prompts_file else ask_prompts_interactive()

    output_dir = Path(args.output_dir) if args.output_dir else (Path.cwd() / 'outputs')
    output_dir.mkdir(parents=True, exist_ok=True)

    jobs = []
    for index, prompt in enumerate(prompts, start=1):
        extension = 'jpg' if mode == 'image' else 'mp4'
        jobs.append(BananaJob(
            mode=mode,
            prompt=prompt,
            model=model,
            aspect_ratio=args.aspect_ratio,
            reference_path=reference_path,
            output_path=str(output_dir / f'banana_{index}.{extension}'),
        ))

    scheduler = BananaScheduler(tokens=tokens, thread_count=thread_count, max_attempts=5)
    try:
        results = scheduler.submit(jobs)
    finally:
        scheduler.shutdown()

    completed = sum(1 for item in results if item.status == 'completed')
    failed = sum(1 for item in results if item.status != 'completed')


if __name__ == '__main__':
    main()
