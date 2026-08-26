from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import time

import pytest

import phadd
from phadd.cli import ExitCode, main


class TestCli:
    def test_version(self, capsys):
        try:
            main(["--version"])
        except SystemExit as exc:
            assert exc.code == 0
        assert phadd.__version__ in capsys.readouterr().out

    def test_missing_hadd_binary(self, tmp_path, monkeypatch, make_root_files):
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        files = make_root_files(tmp_path / "in", 3)

        code = main([str(tmp_path / "out.root"), *map(str, files)])

        assert code == ExitCode.MISSING_HADD

    def test_single_input_is_copied(self, tmp_path, fake_hadd, make_root_files):
        files = make_root_files(tmp_path / "in", 1)
        out = tmp_path / "out.root"

        code = main([str(out), str(files[0])])

        assert code == ExitCode.OK
        assert out.is_file()

    def test_output_same_as_single_input_fails(self, tmp_path, fake_hadd, make_root_files):
        files = make_root_files(tmp_path / "in", 1)

        code = main([str(files[0]), str(files[0])])

        assert code == ExitCode.ERROR

    def test_missing_single_input_fails(self, tmp_path, fake_hadd):
        code = main([str(tmp_path / "out.root"), str(tmp_path / "nope.root")])
        assert code == ExitCode.ERROR

    def test_successful_merge(self, tmp_path, fake_hadd, make_root_files):
        files = make_root_files(tmp_path / "in", 7)
        out = tmp_path / "out.root"

        code = main(["-j", "2", str(out), *map(str, files)])

        assert code == ExitCode.OK
        assert out.is_file()

    def test_existing_output_requires_force(self, tmp_path, fake_hadd, make_root_files):
        files = make_root_files(tmp_path / "in", 3)
        out = tmp_path / "out.root"
        out.touch()

        code = main([str(out), *map(str, files)])
        assert code == ExitCode.ERROR

        code = main(["-f", str(out), *map(str, files)])
        assert code == ExitCode.OK

    def test_glob_expansion(self, tmp_path, fake_hadd, make_root_files):
        make_root_files(tmp_path / "in", 4)
        pattern = str(tmp_path / "in" / "*.root")

        # Pass the unexpanded pattern; expand_inputs should resolve it.
        code = main([str(tmp_path / "out.root"), pattern])

        assert code == ExitCode.OK

    def test_stale_output_excluded_from_inputs(self, tmp_path, fake_hadd, make_root_files):
        files = make_root_files(tmp_path / "in", 3)
        out = tmp_path / "out.root"
        out.write_text("out.root\n")  # stale previous output

        # Simulate shell globbing that picked up the stale output.
        code = main(["-f", str(out), *map(str, [*files, out])])

        assert code == ExitCode.OK
        merged = {line for line in out.read_text().splitlines() if line}
        assert merged == {f.name for f in files}  # 'out.root' not double-counted

    def test_hadd_args_passthrough(self, tmp_path, monkeypatch, make_root_files):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        script = bin_dir / "hadd"
        log_file = tmp_path / "hadd.log"
        # Record every invocation (log path baked into the script so it does
        # not depend on environment inheritance), then behave like fake hadd.
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"with open({str(log_file)!r}, 'a') as log:\n"
            "    log.write('\\x1f'.join(sys.argv[1:]) + '\\n')\n"
            "positional = [a for a in sys.argv[1:] if not a.startswith('-')]\n"
            "with open(positional[0], 'w') as f:\n"
            "    f.write('\\n'.join(sorted(positional[1:])))\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])

        files = make_root_files(tmp_path / "in", 4)
        code = main(["--hadd-args", "-k -v", str(tmp_path / "out.root"), *map(str, files)])

        assert code == ExitCode.OK
        invocations = log_file.read_text().splitlines()
        assert invocations, "hadd was never called"
        for line in invocations:
            args = line.split("\x1f")
            assert args[0] == "-f"  # always forced, never interactive
            assert "-k" in args and "-v" in args

    def test_sigterm_cleans_up_and_exits(self, tmp_path, make_root_files):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        script = bin_dir / "hadd"
        script.write_text("#!/usr/bin/env python3\nimport time; time.sleep(60)\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        files = make_root_files(tmp_path / "in", 6)
        env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
        # Scope intermediates to a known directory (portable across platforms).
        work_dir = tmp_path / "work"
        work_dir.mkdir()

        def temp_dirs():
            return set(work_dir.glob("phadd_*"))

        before = temp_dirs()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "phadd.cli",
                "-j",
                "2",
                "-n",
                "2",
                "-t",
                str(work_dir),
                str(tmp_path / "out.root"),
                *map(str, files),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait until the merge has actually started (temp dir appears).
        deadline = time.monotonic() + 30
        started = False
        while time.monotonic() < deadline:
            if temp_dirs() - before:
                started = True
                break
            if proc.poll() is not None:
                pytest.fail(f"phadd exited early: rc={proc.returncode}")
            time.sleep(0.1)
        if not started:
            pytest.fail("merge never started")

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail(f"phadd hung after SIGTERM; stderr:\n{proc.stderr.read()}")

        err = proc.stderr.read()
        if proc.returncode != 128 + int(signal.SIGTERM):
            pytest.fail(f"unexpected rc={proc.returncode}; stderr:\n{err}")
        assert temp_dirs() == before, "temp dirs leaked"

        # The sleeping stub hadd processes must have been killed too (no orphans).
        if os.name == "posix":
            time.sleep(0.5)  # allow a moment for signal delivery
            pgrep = subprocess.run(
                ["pgrep", "-f", str(bin_dir)],
                capture_output=True,
                text=True,
            )
            assert pgrep.stdout.strip() == "", f"orphaned hadd processes: {pgrep.stdout}"
