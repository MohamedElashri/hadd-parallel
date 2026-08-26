[![CI](https://github.com/MohamedElashri/hadd-parallel/actions/workflows/ci.yml/badge.svg)](https://github.com/MohamedElashri/hadd-parallel/actions/workflows/ci.yml)

# Parallel HAdd (`phadd`)

A small, dependency-free CLI that merges ROOT files in parallel using `hadd`
from [ROOT](https://root.cern). Instead of adding all files in a single pass,
files are merged in chunks across multiple worker processes, then the chunks
are merged again until one file remains — a tree reduction. This keeps memory
usage low and makes use of all your CPU cores.

## Requirements

- Python 3.10+
- ROOT's `hadd` available on your `PATH`

Development and CI run on Linux and macOS; Windows is untested.

## Installation

From PyPI:

```console
pip install parallel-hadd
```

With an optional rich progress bar and colored logs:

```console
pip install "parallel-hadd[rich]"
```

From source:

```console
git clone https://github.com/MohamedElashri/hadd-parallel
cd hadd-parallel
pip install .
```

## Usage

```console
phadd out.root *.root
```

where `out.root` is the merged output file containing all input files.

For more options see the help page:

```console
phadd -h
```

## Help page

```
usage: phadd [-h] [-j NUM_JOBS] [-n NUM_FILES] [-t TMPDIR] [-f] [-s] [--no-progress]
             [--hadd-args ARGS] [-l {DEBUG,INFO,WARNING,ERROR,CRITICAL}] [-V]
             output_file input_file [input_file ...]

Merge ROOT files in parallel using hadd.

positional arguments:
  output_file           path of the merged output file
  input_file            two or more input files (wildcards are expanded)

options:
  -h, --help            show this help message and exit
  -j, --jobs NUM_JOBS   number of parallel hadd processes [default: number of CPUs]
  -n, --num-files NUM_FILES
                        number of files to merge per chunk, minimum 2 [default: auto
                        (~cuberoot of input count, minimum 10)]
  -t, --tmpdir TMPDIR   base directory for intermediate files [default: system temp]
  -f, --force-overwrite
                        overwrite the output file if it exists
  -s, --save-tmp        keep intermediate files instead of deleting them
  --no-progress         disable the progress bar (automatic when not attached to a
                        terminal)
  --hadd-args ARGS      extra flags forwarded verbatim to each inner hadd call; use "="
                        when they start with a dash, e.g. --hadd-args="-k -v"
  -l, --log {DEBUG,INFO,WARNING,ERROR,CRITICAL}
                        log level [default: WARNING]
  -V, --version         show program's version number and exit

Tip: place intermediates on fast local disk with --tmpdir when /tmp is RAM-backed or
slow. See the README for tuning guidance.
```

Inner `hadd` invocations always run with `-f` (never interactive) and are
quieted; their output is shown with `--log DEBUG` or when a merge step fails.

## How it works

Given N input files and a chunk size of `-n`:

1. Files are grouped into chunks of at most `n` files.
2. Each chunk is merged by its own `hadd` process; up to `-j` processes run
   concurrently.
3. The resulting chunk outputs become the inputs of the next round.
4. Rounds repeat until a single file remains, which is moved to the output path.

Intermediate files live in a temporary directory (configurable with `-t`) and
are deleted afterwards unless `-s` is given.

The final output is written atomically (staged next to the destination, then
renamed into place), so an interrupted run can never leave a half-written
output file behind. If the output path is picked up by shell globbing (e.g.
re-running `phadd out.root *.root` after `out.root` already exists), it is
excluded from the input list automatically.

## Tuning & performance

Understanding the trade-offs helps you pick the right flags:

**I/O amplification.** A tree reduction rewrites the surviving data every
round: roughly `log_n(N)` full passes over the dataset for `N` files at chunk
size `n`. Sequential merging writes the data once but cannot be parallelized —
phadd trades extra I/O for wall-clock speed.

**Chunk size (`-n`).** By default phadd picks it automatically — roughly the
cube root of the input count (minimum 10) — which holds the merge at about
three rounds no matter the scale. Override it when your storage calls for
something different:

- **Fast local SSD / NVMe:** defaults are fine; extra passes cost little.
- **Network storage (NFS/Lustre/dCache):** increase `-n` (e.g. `-n 50`) to cut
  the number of rounds and reduce metadata churn; parallelism then comes from
  fewer but larger workers.
- **Many small files:** each inner `hadd` process pays ~0.5–2 s of ROOT startup,
  which dominates when chunks merge in milliseconds. Raise `-n` to amortize it.

**Memory.** Peak usage scales with `jobs × chunk size`: every concurrent `hadd`
holds its chunk's objects in memory. Lower `-j` or `-n` if you hit RAM limits.

**Temporary directory.** On many systems `/tmp` is tmpfs (RAM-backed) or slow.
For large merges place intermediates on fast local disk with `-t /scratch`.

**Tail effect.** The last rounds have few chunks (the final round is a single
task), so some cores idle near the end. This is inherent to tree reduction;
larger `-n` shortens that tail.

**Exit codes.** `0` success, `1` error, `127` `hadd` not found on `PATH`,
`128+N` terminated by signal `N` (intermediate files are cleaned up either way).

## phadd vs native `hadd -j`

Since ROOT 6.24, plain `hadd -j J` merges with multiple threads. For modern
ROOT installations, try it first — it avoids phadd's multi-pass I/O entirely.

phadd is still useful when you:

- run ROOT **older than 6.24**, where `hadd` is single-threaded;
- want **process isolation**: a corrupted input file crashes one chunk worker,
  not the whole merge;
- need to **scale past thread contention** observed by single-process threaded
  merging on very high-core machines or shared/login nodes;
- prefer explicit control over parallelism, temporary storage placement, and
  resumable-by-inspection intermediate trees (`-s`).

## Known limitations

- Input file lists are validated up front; inputs added mid-run will not be
  seen.
- On Linux/macOS, interrupted merges clean up completely: workers and their
  `hadd` children run in dedicated process groups that are killed on
  interruption. On other platforms (e.g. Windows, untested) orphaned child
  processes may be left behind.
- Windows is untested.

## Development

```console
git clone https://github.com/MohamedElashri/hadd-parallel
cd hadd-parallel
pip install -e '.[dev]'
pytest
ruff check . && ruff format --check .
```

Tests use a stub `hadd` executable, so no ROOT installation is required.

## License

[MIT](LICENSE)
