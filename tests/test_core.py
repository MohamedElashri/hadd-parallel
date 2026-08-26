from __future__ import annotations

from pathlib import Path

import pytest

from phadd.core import AUTO_MIN_BATCH, HaddError, ParallelHadd


def read_merged(path: Path) -> set[str]:
    return {line for line in path.read_text().splitlines() if line}


def leaf_names(files: list[Path]) -> set[str]:
    return {f.name for f in files}


class TestValidation:
    def test_requires_two_inputs(self, tmp_path):
        with pytest.raises(ValueError):
            ParallelHadd(output=tmp_path / "out.root", inputs=[tmp_path / "a.root"])

    def test_missing_inputs(self, tmp_path, make_root_files):
        files = make_root_files(tmp_path / "in", 3)
        (tmp_path / "in" / "file_001.root").unlink()
        with pytest.raises(FileNotFoundError):
            ParallelHadd(output=tmp_path / "out.root", inputs=files)

    def test_existing_output_rejected_without_overwrite(self, tmp_path, make_root_files):
        files = make_root_files(tmp_path / "in", 3)
        out = tmp_path / "out.root"
        out.touch()
        with pytest.raises(FileExistsError):
            ParallelHadd(output=out, inputs=files)
        assert ParallelHadd(output=out, inputs=files, overwrite=True) is not None

    def test_output_among_inputs_rejected(self, tmp_path, make_root_files):
        files = make_root_files(tmp_path / "in", 3)
        files.append(tmp_path / "out.root")
        with pytest.raises(ValueError, match="must not be listed among the input"):
            ParallelHadd(output=tmp_path / "out.root", inputs=files)

    def test_invalid_batch_size_and_jobs_rejected(self, tmp_path, make_root_files):
        files = make_root_files(tmp_path / "in", 3)
        with pytest.raises(ValueError, match="batch_size"):
            ParallelHadd(output=tmp_path / "out.root", inputs=files, batch_size=1)
        with pytest.raises(ValueError, match="batch_size"):
            ParallelHadd(output=tmp_path / "out.root", inputs=files, batch_size=-5)
        with pytest.raises(ValueError, match="jobs"):
            ParallelHadd(output=tmp_path / "out.root", inputs=files, jobs=0)


class TestMerge:
    def test_merges_all_files(self, tmp_path, fake_hadd, make_root_files):
        files = make_root_files(tmp_path / "in", 25)
        out = tmp_path / "out.root"

        result = ParallelHadd(output=out, inputs=files, batch_size=10).run()

        assert result == out
        assert out.is_file()
        assert read_merged(out) == leaf_names(files)

    def test_batch_size_smaller_than_input_count_needs_multiple_rounds(
        self, tmp_path, fake_hadd, make_root_files
    ):
        files = make_root_files(tmp_path / "in", 41)
        out = tmp_path / "out.root"

        merger = ParallelHadd(output=out, inputs=files, batch_size=2, keep_tmp=True)
        merger.run()

        # ceil(41/2)=21 -> 11 -> 6 -> 3 -> 2 -> 1 : six rounds
        counts = [len(list((merger.workdir / f"round_{i:03d}").glob("merged_*.root"))) for i in range(6)]
        assert counts == [21, 11, 6, 3, 2, 1]
        assert read_merged(out) == leaf_names(files)

    def test_single_round_when_all_fit_in_one_batch(self, tmp_path, fake_hadd, make_root_files):
        files = make_root_files(tmp_path / "in", 5)
        out = tmp_path / "out.root"

        ParallelHadd(output=out, inputs=files, batch_size=10).run()

        assert read_merged(out) == leaf_names(files)

    def test_auto_batch_size_keeps_reduction_shallow(self, tmp_path, fake_hadd, make_root_files):
        n = 8000
        files = make_root_files(tmp_path / "in", n)
        out = tmp_path / "out.root"

        merger = ParallelHadd(output=out, inputs=files, batch_size=None, keep_tmp=True)
        merger.run()

        # auto chunk size for 8000 inputs is ceil(8000**(1/3))=20 -> 3 rounds
        assert merger.batch_size == 20
        counts = [len(list((merger.workdir / f"round_{i:03d}").glob("merged_*.root"))) for i in range(3)]
        assert counts == [400, 20, 1]
        assert read_merged(out) == leaf_names(files)

    def test_auto_batch_size_floors_at_minimum(self, tmp_path, fake_hadd, make_root_files):
        files = make_root_files(tmp_path / "in", 30)
        out = tmp_path / "out.root"

        merger = ParallelHadd(output=out, inputs=files)
        merger.run()

        assert merger.batch_size == AUTO_MIN_BATCH
        assert read_merged(out) == leaf_names(files)

    def test_parallel_matches_serial(self, tmp_path, fake_hadd, make_root_files):
        files = make_root_files(tmp_path / "in", 30)

        serial_out = tmp_path / "serial.root"
        parallel_out = tmp_path / "parallel.root"
        ParallelHadd(output=serial_out, inputs=files, jobs=1).run()
        ParallelHadd(output=parallel_out, inputs=files, jobs=4).run()

        assert read_merged(serial_out) == read_merged(parallel_out) == leaf_names(files)

    def test_progress_callback_invoked(self, tmp_path, fake_hadd, make_root_files):
        files = make_root_files(tmp_path / "in", 12)
        calls: list[tuple[int, int]] = []

        ParallelHadd(output=tmp_path / "out.root", inputs=files, batch_size=10).run(
            progress=lambda done, total: calls.append((done, total))
        )

        # round 1: two chunks of 10 and 2; round 2: one chunk
        assert calls == [(1, 2), (2, 2), (1, 1)]
        assert all(done <= total for done, total in calls)

    def test_keep_tmp_preserves_intermediates(self, tmp_path, fake_hadd, make_root_files):
        files = make_root_files(tmp_path / "in", 4)
        out = tmp_path / "out.root"

        merger = ParallelHadd(output=out, inputs=files, keep_tmp=True)
        merger.run()

        assert merger.workdir is not None and merger.workdir.is_dir()


class TestFinalize:
    def test_output_replaces_existing_file(self, tmp_path, fake_hadd, make_root_files):
        files = make_root_files(tmp_path / "in", 4)
        out = tmp_path / "out.root"
        out.write_text("stale content\n")

        ParallelHadd(output=out, inputs=files, overwrite=True).run()

        assert read_merged(out) == leaf_names(files)
        # No staging leftovers.
        assert not list(out.parent.glob(".*.phadd-tmp"))

    def test_staging_cleaned_up_on_move_failure(self, tmp_path, fake_hadd, make_root_files):
        files = make_root_files(tmp_path / "in", 4)

        merger = ParallelHadd(output=tmp_path / "nonexistent-dir" / "out.root", inputs=files)
        with pytest.raises(OSError):
            merger.run()

        assert not list(tmp_path.glob(".*.phadd-tmp"))


class TestHaddError:
    def test_failing_hadd_propagates_with_captured_output(self, tmp_path, fake_hadd, make_root_files):
        # Overwrite the stub so it prints to stderr and exits nonzero.
        fake_hadd.write_text('#!/bin/sh\necho "something went wrong" >&2\nexit 3\n')
        fake_hadd.chmod(0o755)
        files = make_root_files(tmp_path / "in", 4)

        with pytest.raises(HaddError, match=r"(?s)exit code 3.*something went wrong"):
            ParallelHadd(output=tmp_path / "out.root", inputs=files).run()

    def test_tmp_cleaned_up_after_failure(self, tmp_path, fake_hadd, make_root_files):
        fake_hadd.write_text("#!/bin/sh\nexit 1\n")
        fake_hadd.chmod(0o755)
        files = make_root_files(tmp_path / "in", 4)

        merger = ParallelHadd(output=tmp_path / "out.root", inputs=files)
        with pytest.raises(HaddError):
            merger.run()

        assert merger.workdir is None or not merger.workdir.exists()
