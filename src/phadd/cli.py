"""Command-line interface for phadd."""

from __future__ import annotations

import argparse
import logging
import multiprocessing
import shlex
import shutil
import signal
import sys
from glob import glob
from pathlib import Path

from phadd import __version__
from phadd.core import AUTO_MIN_BATCH, HaddError, ParallelHadd

try:
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.progress import Progress

    HAS_RICH = True
except ImportError:  # pragma: no cover - depends on environment
    HAS_RICH = False

logger = logging.getLogger(__name__)


class ExitCode:
    OK = 0
    ERROR = 1
    MISSING_HADD = 127  # shell convention for "command not found"


class _Terminated(KeyboardInterrupt):
    """Raised by signal handlers so cleanup (finally blocks) still runs."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"terminated by signal {signum}")
        self.signum = signum


def _install_signal_handlers() -> None:
    def handler(signum: int, frame: object) -> None:
        raise _Terminated(signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


def expand_inputs(patterns: list[str]) -> list[str]:
    """Expand shell-like wildcards that the user's shell did not expand."""
    expanded: list[str] = []
    for pattern in patterns:
        matches = glob(pattern)
        if any(c in pattern for c in "*?[") and matches:
            expanded.extend(matches)
        else:
            expanded.append(pattern)
    return expanded


def default_jobs() -> int:
    try:
        return max(1, multiprocessing.cpu_count())
    except NotImplementedError:  # pragma: no cover
        return 1


def build_parser(default_jobs: int) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phadd",
        description="Merge ROOT files in parallel using hadd.",
        epilog="Tip: place intermediates on fast local disk with --tmpdir when /tmp "
        "is RAM-backed or slow. See the README for tuning guidance.",
    )
    parser.add_argument(
        "output_file",
        type=str,
        help="path of the merged output file",
    )
    parser.add_argument(
        "input_files",
        type=str,
        nargs="+",
        metavar="input_file",
        help="two or more input files (wildcards are expanded)",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        dest="num_jobs",
        default=default_jobs,
        help=f"number of parallel hadd processes [default: {default_jobs}]",
    )
    parser.add_argument(
        "-n",
        "--num-files",
        type=int,
        dest="num_files",
        default=None,
        help=f"number of files to merge per chunk, minimum 2 "
        f"[default: auto (~cuberoot of input count, minimum {AUTO_MIN_BATCH})]",
    )
    parser.add_argument(
        "-t",
        "--tmpdir",
        type=str,
        default=None,
        help="base directory for intermediate files [default: system temp]",
    )
    parser.add_argument(
        "-f",
        "--force-overwrite",
        action="store_true",
        help="overwrite the output file if it exists",
    )
    parser.add_argument(
        "-s",
        "--save-tmp",
        action="store_true",
        help="keep intermediate files instead of deleting them",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="disable the progress bar (automatic when not attached to a terminal)",
    )
    parser.add_argument(
        "--hadd-args",
        type=str,
        default="",
        metavar="ARGS",
        help='extra flags forwarded verbatim to each inner hadd call; use "=" when they '
        'start with a dash, e.g. --hadd-args="-k -v"',
    )
    parser.add_argument(
        "-l",
        "--log",
        dest="log_level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="log level [default: WARNING]",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def configure_logging(level: str) -> None:
    if HAS_RICH:
        logging.basicConfig(
            level=level,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
        )
    else:
        logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def exclude_output(inputs: list[str], output: str) -> list[str]:
    """Drop any input that refers to the output file itself (e.g. a stale
    ``out.root`` picked up by shell globbing); merging it would silently
    double-count the data."""
    out_path = Path(output).resolve()
    kept = [p for p in inputs if Path(p).resolve() != out_path]
    if len(kept) != len(inputs):
        logger.warning("Excluding output file from the input list: %s", output)
    return kept


def main(argv: list[str] | None = None) -> int:
    _install_signal_handlers()
    args = build_parser(default_jobs()).parse_args(argv)
    configure_logging(args.log_level)
    logger.debug("args: %s", args)

    if shutil.which("hadd") is None:
        logger.error("'hadd' not found on PATH. Please install ROOT: https://root.cern")
        return ExitCode.MISSING_HADD

    inputs = exclude_output(expand_inputs(args.input_files), args.output_file)

    if not inputs:
        logger.error("no input files remain after excluding the output file")
        return ExitCode.ERROR

    if len(inputs) == 1:
        src = Path(inputs[0])
        if not src.is_file():
            logger.error("input file not found: %s", src)
            return ExitCode.ERROR
        logger.warning("Only one input file given; copying it to the output")
        try:
            shutil.copy2(src, args.output_file)
        except OSError as exc:
            logger.error("could not copy %s: %s", src, exc)
            return ExitCode.ERROR
        logger.info("Copied %s -> %s", src, args.output_file)
        return ExitCode.OK

    try:
        merger = ParallelHadd(
            output=args.output_file,
            inputs=inputs,
            batch_size=args.num_files,
            jobs=args.num_jobs,
            tmp_dir=args.tmpdir,
            keep_tmp=args.save_tmp,
            overwrite=args.force_overwrite,
            hadd_args=shlex.split(args.hadd_args),
        )
    except FileExistsError as exc:
        logger.error("%s (use --force-overwrite to replace it)", exc)
        return ExitCode.ERROR
    except (ValueError, FileNotFoundError) as exc:
        logger.error(str(exc))
        return ExitCode.ERROR

    use_bar = not args.no_progress and sys.stderr.isatty()
    progress = _make_progress(use_bar)

    try:
        with progress.context if progress else _nullcontext():
            merger.run(progress=progress.report if progress else None)
    except HaddError as exc:
        logger.error("%s", exc)
        return ExitCode.ERROR
    except OSError as exc:
        logger.error("%s", exc)
        return ExitCode.ERROR
    except KeyboardInterrupt as exc:
        signum = getattr(exc, "signum", signal.SIGINT)
        logger.error("interrupted (signal %d); temporary files were cleaned up", signum)
        return 128 + int(signum)

    logger.info("Done: %s", args.output_file)
    return ExitCode.OK


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Progress:
    __slots__ = ("context", "report")

    def __init__(self, context, report) -> None:
        self.context = context
        self.report = report


def _make_progress(enabled: bool) -> _Progress | None:
    """Build a rich-backed progress reporter, or None when disabled."""
    if enabled and HAS_RICH:
        console = Console(stderr=True)
        progress = Progress(console=console, transient=True)
        task_id = progress.add_task("merging", total=None)

        def report(completed: int, total: int) -> None:
            progress.update(task_id, completed=completed, total=total)

        return _Progress(progress, report)
    return None


if __name__ == "__main__":
    sys.exit(main())
