"""Core logic for merging ROOT files in parallel with hadd."""

from __future__ import annotations

import contextlib
import logging
import math
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["AUTO_MIN_BATCH", "HaddError", "ParallelHadd"]

logger = logging.getLogger(__name__)

AUTO_MIN_BATCH = 10
"""Minimum chunk size when batch_size is chosen automatically."""

ProgressCallback = Callable[[int, int], None]
"""Called as ``progress(completed, total)`` after each chunk of a round finishes."""


class HaddError(RuntimeError):
    """Raised when an underlying hadd invocation fails."""


def _auto_batch_size(n_inputs: int) -> int:
    """Pick a chunk size that keeps the tree reduction at about three rounds.

    With chunk size ~cuberoot(N), N files reduce in ~3 rounds no matter how
    large the input list grows.
    """
    return max(AUTO_MIN_BATCH, math.ceil(n_inputs ** (1 / 3)))


def _worker_init() -> None:
    """Pool worker startup (POSIX).

    Each worker gets its own process group and default signal dispositions:

    - The group makes cleanup atomic: ``killpg`` kills the worker *and* any
      hadd child it has running -- even one caught mid fork/exec -- with no
      dependence on handlers or timing.
    - Default dispositions undo handlers inherited via fork from the CLI
      (a worker that raises on SIGTERM instead of dying would stall shutdown).
    """
    if os.name == "posix":
        os.setpgrp()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_DFL)


def _set_pdeathsig() -> None:
    """preexec_fn: ask Linux to SIGKILL hadd if its worker dies.

    Belt-and-braces for paths that bypass group cleanup entirely, such as a
    worker being OOM-killed while the pool keeps running.
    """
    if sys.platform.startswith("linux"):
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG


def _merge_chunk(output: str, inputs: Sequence[str], extra_args: Sequence[str] = ()) -> str:
    """Worker entry point: merge ``inputs`` into ``output`` using hadd.

    ``-f`` is always passed so hadd can never block on an interactive
    overwrite prompt. ``extra_args`` are forwarded verbatim after ``-f``.

    Output is captured so concurrent workers do not garble the terminal; it is
    logged at DEBUG level, or included in the error on failure.
    """
    cmd = ["hadd", "-f", *extra_args, output, *inputs]
    logger.debug("Running: %s", " ".join(cmd))

    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["preexec_fn"] = _set_pdeathsig
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **popen_kwargs,
    )
    try:
        hadd_output, _ = proc.communicate()
    except BaseException:
        with contextlib.suppress(Exception):
            proc.kill()
            proc.wait(timeout=5)
        raise

    hadd_output = (hadd_output or "").strip()
    if hadd_output:
        logger.debug("hadd output:\n%s", hadd_output)
    if proc.returncode != 0:
        msg = f"hadd failed (exit code {proc.returncode}): {' '.join(cmd)}"
        if hadd_output:
            msg += f"\n{hadd_output[-2000:]}"
        raise HaddError(msg)
    return output


def _kill_pool_workers(pool: ProcessPoolExecutor) -> None:
    """Stop the pool deterministically: SIGKILL each worker's process group.

    Workers run in their own groups (see ``_worker_init``), so a single
    ``killpg`` takes out the worker and any hadd child it has running — even
    one caught mid fork/exec. SIGKILL cannot be blocked or handled, and dead
    workers cannot stall ``shutdown(wait=True)`` during interpreter exit.
    """
    workers = list((getattr(pool, "_processes", None) or {}).values())
    for proc in workers:
        with contextlib.suppress(Exception):
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
    pool.shutdown(wait=False, cancel_futures=True)


def _batched(items: Sequence[Path], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


@dataclass
class ParallelHadd:
    """Merge a list of ROOT files into one via tree-reduction of parallel hadd runs.

    Each round, files are grouped into chunks of ``batch_size`` and every chunk is
    merged by its own hadd process. Rounds repeat until a single file remains.
    """

    output: Path
    inputs: list[Path]
    batch_size: int | None = None
    """Files per chunk; ``None`` picks a size automatically (~cuberoot of the
    input count, minimum :data:`AUTO_MIN_BATCH`) so large merges stay at about
    three rounds."""
    jobs: int = 1
    tmp_dir: Path | None = None
    keep_tmp: bool = False
    overwrite: bool = False
    hadd_args: list[str] = field(default_factory=list)
    """Extra flags forwarded verbatim to every inner hadd invocation."""
    _workdir: Path | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.output = Path(self.output).resolve()
        self.inputs = [Path(p) for p in self.inputs]
        self.tmp_dir = Path(self.tmp_dir) if self.tmp_dir else None

        if len(self.inputs) < 2:
            msg = "at least two input files are required"
            raise ValueError(msg)
        if self.output in [Path(p).resolve() for p in self.inputs]:
            msg = "the output file must not be listed among the input files"
            raise ValueError(msg)
        missing = [p for p in self.inputs if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"input file(s) not found: {', '.join(map(str, missing))}")
        if self.output.exists() and not self.overwrite:
            raise FileExistsError(f"output file already exists: {self.output}")

        if self.batch_size is None:
            self.batch_size = _auto_batch_size(len(self.inputs))
        if int(self.batch_size) < 2:
            msg = f"batch_size must be at least 2, got {self.batch_size}"
            raise ValueError(msg)
        if int(self.jobs) < 1:
            msg = f"jobs must be at least 1, got {self.jobs}"
            raise ValueError(msg)
        self.batch_size = int(self.batch_size)
        self.jobs = int(self.jobs)

    @property
    def workdir(self) -> Path | None:
        """Directory holding intermediate files; only valid during/after run()."""
        return self._workdir

    def run(self, progress: ProgressCallback | None = None) -> Path:
        """Run the merge and return the path of the final output file."""
        self._workdir = Path(tempfile.mkdtemp(prefix="phadd_", dir=self.tmp_dir))
        logger.info("Using temporary directory: %s", self._workdir)

        try:
            current = list(self.inputs)
            step = 0
            while len(current) > 1:
                logger.info("Round %d: merging %d files", step + 1, len(current))
                current = self._merge_round(current, self._workdir / f"round_{step:03d}", progress)
                step += 1

            self._finalize(current[0])
            return self.output
        finally:
            if not self.keep_tmp:
                logger.debug("Removing temporary directory %s", self._workdir)
                shutil.rmtree(self._workdir, ignore_errors=True)
            else:
                logger.info("Intermediate files kept in %s", self._workdir)

    def _merge_round(
        self, current: list[Path], out_dir: Path, progress: ProgressCallback | None
    ) -> list[Path]:
        """Merge all files in ``current`` into new chunk files inside ``out_dir``."""
        out_dir.mkdir(parents=True)
        chunks = list(_batched(current, self.batch_size))

        if self.jobs > 1:
            results = self._merge_parallel(chunks, out_dir, progress)
        else:
            results = []
            for done, chunk in enumerate(chunks, start=1):
                dest = out_dir / f"merged_{done - 1:06d}.root"
                _merge_chunk(str(dest), [str(p) for p in chunk], self.hadd_args)
                results.append(dest)
                if progress is not None:
                    progress(done, len(chunks))

        return sorted(results)

    def _merge_parallel(
        self, chunks: list[list[Path]], out_dir: Path, progress: ProgressCallback | None
    ) -> list[Path]:
        """Merge chunks across a process pool, returning destinations in order."""
        jobs = min(self.jobs, len(chunks))
        # Fork deliberately: workers must inherit the live environment (PATH
        # etc.). Python 3.14 switched Linux's default to forkserver, whose
        # workers fork from a stale server snapshot instead.
        mp_context = multiprocessing.get_context("fork") if os.name == "posix" else None
        with ProcessPoolExecutor(max_workers=jobs, initializer=_worker_init, mp_context=mp_context) as pool:
            futures = {
                pool.submit(
                    _merge_chunk,
                    str(out_dir / f"merged_{i:06d}.root"),
                    [str(p) for p in chunk],
                    self.hadd_args,
                ): i
                for i, chunk in enumerate(chunks)
            }
            try:
                for completed, future in enumerate(as_completed(futures), start=1):
                    try:
                        future.result()
                    except BrokenProcessPool as exc:
                        msg = "a merge worker process died unexpectedly (killed or out of memory)"
                        raise HaddError(msg) from exc
                    if progress is not None:
                        progress(completed, len(chunks))
            except BaseException:
                # Don't let queued chunks start after a failure or interrupt,
                # and stop already-running workers promptly (fail fast).
                for future in futures:
                    future.cancel()
                _kill_pool_workers(pool)
                raise

        return sorted(out_dir.glob("merged_*.root"))

    def _finalize(self, last_file: Path) -> None:
        """Move/copy the final merged file to its destination atomically.

        The file is staged next to the output (same filesystem) and then put
        in place with ``os.replace``, so an interrupted run can never leave a
        truncated or half-written output file behind.
        """
        logger.info("Writing final output: %s", self.output)
        staging = self.output.parent / f".{self.output.name}.phadd-tmp"
        try:
            if self.keep_tmp:
                shutil.copy2(last_file, staging)
            else:
                shutil.move(str(last_file), str(staging))
            os.replace(staging, self.output)
        except BaseException:
            staging.unlink(missing_ok=True)
            raise
