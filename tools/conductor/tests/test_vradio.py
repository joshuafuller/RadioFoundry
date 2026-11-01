# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Launcher checks, including real socket tests that must also run outside sandboxes."""

import errno
import http.cookiejar
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from tools import vradio
from tools.conductor.conductor import install_parts
from tools.conductor.parts import frames
from tools.conductor.world import Config


@pytest.mark.parametrize("options", [
    ["--radios", "0"], ["--radios", "-1"], ["--radios", "many"], ["--radios", "17"],
    ["--ratio", "0"], ["--ratio", "nan"], ["--ratio", "inf"], ["--ratio", "fast"],
    ["--until-ms", "0"], ["--until-ms", str(1 << 64)], ["--name", "../escape"],
])
def test_bad_arguments(options, capsys):
    with pytest.raises(SystemExit) as caught:
        vradio.main(["up", *options])
    assert caught.value.code == 2
    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1 and "--help" in error
    if options == ["--radios", "0"]:
        with capsys.disabled():
            print(f"\n--radios 0 (exit 2): {error}", end="")


def test_defaults_and_options():
    args = vradio.arguments(["up"])
    assert (args.radios, args.ratio, args.no_build, args.no_browser, args.until_ms) == (1, 1, False, False, None)
    assert not args.keep
    args = vradio.arguments(["up", "--radios", "2", "--ratio", "max", "--name", "pair", "--no-build",
                             "--no-browser", "--until-ms", "2000", "--scenario", "pair.json"])
    assert (args.radios, args.ratio, args.name, args.until_ms, args.scenario) == (2, "max", "pair", 2000, Path("pair.json"))
    assert args.no_build and args.no_browser
    assert vradio.arguments(["up", "--ratio", "0.5"]).ratio == 0.5


@pytest.mark.parametrize("override, count", [([], 3), (["--radios", "2"], 2)])
def test_scenario_radio_count_and_explicit_override(tmp_path, override, count):
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"version": 1, "radios": 3, "inputs": [], "end_us": 3000000}))
    args = vradio.arguments(["up", "--scenario", str(path), "--build", f"{count}=other.elf", *override])
    config = vradio.configuration(args, Path("default.elf"))
    assert args.radios == config.radios == count
    assert config.executable_for(count) == "other.elf"
    assert config.end_us == 3000000


def test_scenario_without_radio_count_defaults_to_one(tmp_path):
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"version": 1, "inputs": [], "end_us": 1000}))
    args = vradio.arguments(["up", "--scenario", str(path)])
    assert vradio.configuration(args, Path("radio.elf")).radios == 1


@pytest.mark.parametrize("count", [0, -1, True, 1.5, "2", None, 17])
def test_scenario_bad_radio_count_is_refused(tmp_path, count, capsys):
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"version": 1, "radios": count, "inputs": [], "end_us": 1000}))
    with pytest.raises(SystemExit) as caught:
        vradio.arguments(["up", "--scenario", str(path)])
    assert caught.value.code == 2
    assert "radios" in capsys.readouterr().err


@pytest.mark.parametrize("content", [None, "{", "[]"])
def test_scenario_count_unreadable_file_is_refused(tmp_path, content, capsys):
    path = tmp_path / "scenario.json"
    if content is not None:
        path.write_text(content)
    with pytest.raises(SystemExit) as caught:
        vradio.arguments(["up", "--scenario", str(path)])
    assert caught.value.code == 2
    assert "scenario is invalid or unreadable" in capsys.readouterr().err


@pytest.fixture
def build_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(vradio, "ROOT", tmp_path)
    source = tmp_path / "source.c"
    header = tmp_path / "source.h"
    asset = tmp_path / "page.html"
    for path in (source, header, asset):
        path.touch()
    (tmp_path / "Makefile").write_text(
        "build/vhost/rfoundry_vhost.elf: source.c source.h page.html\n"
        "\t@touch $@\n\t@echo built >> calls\n")
    return tmp_path


def test_build_skip_with_fake_clock(build_tree, monkeypatch):
    # Explicit file times avoid sleeps and prove Make sees source, header and asset changes.
    clock = 1_600_000_000
    monkeypatch.setenv("MAKEFLAGS", "CC=missing-compiler")
    elf = vradio.build(False)
    calls = build_tree / "calls"
    assert calls.read_text().splitlines() == ["built"]
    for index, filename in enumerate(("source.c", "source.h", "page.html"), 2):
        clock += 2
        for item in ("source.c", "source.h", "page.html", "Makefile"):
            os.utime(build_tree / item, (clock - 1, clock - 1))
        os.utime(elf, (clock, clock))
        vradio.build(False)
        assert len(calls.read_text().splitlines()) == index - 1
        os.utime(build_tree / filename, (clock + 1, clock + 1))
        vradio.build(False)
        assert len(calls.read_text().splitlines()) == index


def test_no_build_never_invokes_make(build_tree, monkeypatch):
    monkeypatch.setattr(vradio.subprocess, "run", lambda *a, **k: pytest.fail("unexpected build"))
    with pytest.raises(vradio.LauncherError, match="without --no-build"):
        vradio.build(True)
    elf = build_tree / "build/vhost/rfoundry_vhost.elf"
    elf.parent.mkdir(parents=True)
    elf.touch()
    assert vradio.build(True) == elf


def test_current_build_needs_no_compiler(build_tree, monkeypatch):
    elf = vradio.build(False)
    real_which = vradio.shutil.which
    monkeypatch.setattr(vradio.shutil, "which", lambda name: None if name == "gcc" else real_which(name))
    assert vradio.build(False) == elf
    assert (build_tree / "calls").read_text() == "built\n"


def test_dirty_build_time_does_not_force_rebuild(build_tree):
    (build_tree / "Makefile").write_text(
        "RFOUNDRY_GIT_TIME := 2000\n"
        "$(shell echo 'dirty $(RFOUNDRY_GIT_TIME)' | cmp -s - build/vhost/version || "
        "{ echo 'dirty $(RFOUNDRY_GIT_TIME)' > build/vhost/version; rm -f build/vhost/rfoundry_vhost.elf; })\n"
        "build/vhost/rfoundry_vhost.elf: source.c\n\t@touch $@\n\t@echo built >> calls\n")
    elf = vradio.build(False)
    (elf.parent / "version").write_text("dirty 1000\n")
    assert vradio.build(False) == elf
    assert (build_tree / "calls").read_text() == "built\n"


def test_missing_gcc_message(build_tree, monkeypatch, capsys):
    real_which = vradio.shutil.which
    monkeypatch.setattr(vradio.shutil, "which", lambda name: None if name == "gcc" else real_which(name))
    assert vradio.main(["up", "--no-browser"]) == 1
    error = capsys.readouterr().err
    assert error == "vradio: gcc is missing; install GCC and retry up.\n"
    with capsys.disabled():
        print(f"\nno gcc (exit 1): {error}", end="")


def test_build_failure_keeps_diagnostic(build_tree, capsys):
    (build_tree / "Makefile").write_text("build/vhost/rfoundry_vhost.elf:\n\t@echo broken dependency\n\t@false\n")
    assert vradio.main(["up"]) == 1
    assert "launcher-build.log" in capsys.readouterr().err
    assert "broken dependency" in (build_tree / "build/vhost/launcher-build.log").read_text()


@pytest.fixture
def launcher_world(stub_world, tmp_path, monkeypatch):
    world = stub_world(radios=2, end_us=2000)
    monkeypatch.setattr(vradio, "World", lambda config: world)
    monkeypatch.setattr(vradio, "build", lambda no_build: Path("unused.elf"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    def bind(name, runtime):
        directory = runtime / "rfoundry-vr" / name
        directory.mkdir(parents=True, mode=0o700)
        world.api.directory = directory
        return directory / "world.sock"
    monkeypatch.setattr(world.api, "bind", bind)
    monkeypatch.setattr(frames.Frames, "bind", lambda self: None)
    # Keep the real parts and leases; only their host socket endpoints are fakes.
    def enable(world, enabled):
        def token(request):
            radio = request["radio"]
            return {"principal": f"browser:s{radio}", "url": f"http://127.0.0.1:{40000 + radio}/#rf-token=test"}
        return SimpleNamespace(token=token, status=lambda: {"1": {"state": "up"}, "2": {"state": "up"}})
    monkeypatch.setattr(vradio.webgw, "get", enable)
    return world


def test_launch_uses_conductor_leases_and_cleans_up(launcher_world, monkeypatch, capsys):
    world = launcher_world
    args = vradio.arguments(["up", "--name", "pair", "--radios", "2", "--ratio", "max", "--no-browser", "--keep"])
    monkeypatch.setattr(vradio.webbrowser, "open", lambda url: pytest.fail("--no-browser ignored"))
    assert vradio.up(args) == 0
    output = capsys.readouterr().out
    assert "world folder:" in output and "ended: virtual time limit" in output
    assert output.count("http://127.0.0.1:") == 2
    assert "lease holder: browser:s1" in output and "lease holder: browser:s2" in output
    assert world.state == "Ended" and not world.radios
    audit = [json.loads(line) for line in (world.api.directory / "audit.jsonl").read_text().splitlines()]
    grants = [line for line in audit if line["kind"] == "lease_grant"]
    assert [line["principal"] for line in grants] == ["browser:s1", "browser:s2"]


@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("stop", [False, True])
def test_clean_end_removes_world_unless_kept(launcher_world, monkeypatch, keep, stop):
    world = launcher_world
    if stop:
        monkeypatch.setattr(world, "advance", world.end)
    assert vradio.main(["up", "--name", "demo", "--no-browser", *(["--keep"] if keep else [])]) == 0
    assert world.api.directory.exists() == keep
    if keep:
        assert (world.api.directory / "events.jsonl").is_file()
        assert (world.api.directory / "audit.jsonl").is_file()


def test_failed_world_keeps_logs(launcher_world, monkeypatch, capsys):
    world = launcher_world
    monkeypatch.setattr(world, "advance", lambda: world.end("WORLD_RADIO_FAILURE:test"))
    assert vradio.main(["up", "--no-browser"]) == 1
    assert "WORLD_RADIO_FAILURE:test" in capsys.readouterr().err
    assert (world.api.directory / "events.jsonl").is_file()


def test_existing_world_refused_with_exact_cleanup_command(launcher_world, tmp_path, monkeypatch, capsys):
    root = tmp_path / "runtime with spaces"
    root.mkdir(mode=0o700)
    folder = root / "rfoundry-vr/demo"
    folder.mkdir(parents=True, mode=0o700)
    folder.parent.chmod(0o700)
    log = folder / "events.jsonl"
    log.write_text("keep this recording\n")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root))
    api = launcher_world.api
    monkeypatch.setattr(api, "bind", lambda *args: type(api).bind(api, *args))
    assert vradio.main(["up", "--name", "demo", "--no-browser"]) == 1
    error = capsys.readouterr().err
    assert "already exists" in error and "confirm it is stopped" in error
    assert error.endswith(f"rm -rf -- {shlex.quote(str(folder))}\n")
    assert log.read_text() == "keep this recording\n"


def test_busy_port_closes_world(launcher_world, monkeypatch, capsys):
    def busy():
        raise OSError(errno.EADDRINUSE, "Address already in use")
    monkeypatch.setattr(launcher_world, "start", busy)
    assert vradio.main(["up", "--no-browser"]) == 1
    assert launcher_world.state == "Ended"
    assert launcher_world.api.directory.is_dir()
    error = capsys.readouterr().err
    assert error == "vradio: a gateway port is busy; stop the process using that port and retry up.\n"
    with capsys.disabled():
        print(f"\nbusy gateway port, injected EADDRINUSE (exit 1): {error}", end="")


def test_ctrl_c_and_browser(launcher_world, monkeypatch, capsys):
    world = launcher_world
    opened = []
    monkeypatch.setattr(vradio.webbrowser, "open", lambda url: opened.append(url) or True)
    calls = 0
    def advance():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(world, "advance", advance)
    before = signal.getsignal(signal.SIGTERM)
    assert vradio.main(["up"]) == 0
    assert len(opened) == 2 and not world.radios
    assert signal.getsignal(signal.SIGTERM) == before
    assert "ended: stopped" in capsys.readouterr().out
    assert not world.api.directory.exists()


def test_scenario_limit_and_bad_file(tmp_path):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"version": 1, "inputs": [], "end_us": 3000000}))
    args = vradio.arguments(["up", "--scenario", str(scenario), "--until-ms", "2000", "--radios", "2"])
    config = vradio.configuration(args, Path("radio.elf"))
    assert config == Config(executable="radio.elf", radios=2, end_us=2000000)
    args.until_ms = 4000
    assert vradio.configuration(args, Path("radio.elf")).end_us == 3000000
    scenario.write_text("[]")
    with pytest.raises(vradio.LauncherError, match="version 1"):
        vradio.configuration(args, Path("radio.elf"))


def test_runtime_and_world_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert vradio.main(["status"]) == 0
    assert capsys.readouterr().out == "no running worlds\n"
    assert vradio.main(["down", "--name", "absent"]) == 1
    assert "use status" in capsys.readouterr().err
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(public))
    assert vradio.main(["status"]) == 1
    assert "mode 0700" in capsys.readouterr().err


def test_status_ignores_stale_world_and_down_uses_api(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    parent = tmp_path / "rfoundry-vr"
    parent.mkdir(mode=0o700)
    for name in ("live", "stale"):
        (parent / name).mkdir(mode=0o700)
        (parent / name / "world.sock").touch()
    calls = []
    class Client:
        def __init__(self, path):
            if path.parent.name == "stale":
                raise ConnectionRefusedError(errno.ECONNREFUSED, "stale")
            self.sock = SimpleNamespace(settimeout=lambda seconds: None, recv=lambda size: b"")
        def call(self, command):
            calls.append(command)
            return {"state": "Running", "radios": [1, 2], "time_us": 1000}
        def close(self):
            calls.append("close")
    monkeypatch.setattr(vradio, "WorldClient", Client)
    assert vradio.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "live: Running, 2 radios, 1 ms" in output and "stale:" not in output
    assert vradio.main(["down", "--name", "live"]) == 0
    assert capsys.readouterr().out == "ended: live\n"
    assert calls == ["status", "close", "end", "close"]
    assert (parent / "live").exists()


@pytest.mark.parametrize("keep", [False, True])
def test_live_world_bounded(vhost_elf, tmp_path, capsys, keep):
    start = time.monotonic()
    for _ in range(1 if keep else 2):
        result = subprocess.run([sys.executable, str(vradio.ROOT / "tools/vradio.py"), "up", "--radios", "2",
                                 "--ratio", "max", "--until-ms", "2000", "--no-browser", "--name", "demo",
                                 *(["--keep"] if keep else [])],
                                env={**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)},
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "world folder:" in result.stdout and "ended: virtual time limit" in result.stdout
        assert result.stdout.count("http://127.0.0.1:") == 2
        assert result.stdout.count("lease holder: browser:") == 2
        folder = tmp_path / "rfoundry-vr/demo"
        assert folder.exists() == keep
        if keep:
            assert (folder / "recording/scenario.json").is_file()
            assert not (folder / "world.sock").exists()
    with capsys.disabled():
        print(f"\nbounded world: {time.monotonic() - start:.3f} s\n{result.stdout}")


def test_live_world_browser_status_down(vhost_elf, tmp_path, capsys):
    command = [sys.executable, str(vradio.ROOT / "tools/vradio.py")]
    env = {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)}
    process = subprocess.Popen([*command, "up", "--radios", "2", "--name", "live", "--no-browser"],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output = b""
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 60
            while b"radio 2:" not in output:
                assert time.monotonic() < deadline, output.decode()
                ready = selector.select(1)
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    pytest.fail((output + stdout + stderr).decode())
                if not ready:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    stdout, stderr = process.communicate(timeout=5)
                    pytest.fail((output + stdout + stderr).decode())
                output += chunk
        url = next(line.split()[2] for line in output.decode().splitlines() if line.startswith("radio 1:"))
        # The gateway removes the one-time token with a redirect and needs its cookie on the next request.
        browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        page = url.split("?", 1)[0]
        status = None
        while time.monotonic() < deadline:
            try:
                with browser.open(url, timeout=5) as response:
                    status = response.status
                    assert response.read()
                break
            except urllib.error.HTTPError as error:
                assert error.code == 503
                url = page
        assert status == 200
        listing = subprocess.run([*command, "status"], env=env, capture_output=True, text=True, timeout=10)
        assert listing.returncode == 0 and "live: Running, 2 radios" in listing.stdout
        stopped = subprocess.run([*command, "down", "--name", "live"], env=env,
                                 capture_output=True, text=True, timeout=30)
        assert stopped.returncode == 0, stopped.stderr
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr.decode()
        assert b"ended: stopped" in stdout
        assert not (tmp_path / "rfoundry-vr/live").exists()
        with capsys.disabled():
            print(f"\nbrowser HTTP {status}\n{listing.stdout}{stopped.stdout}launcher exit {process.returncode}")
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=30)


def test_air_settings_group_by_default_and_scenario_air(tmp_path, stub_world):
    """Two or more radios with no scenario share one talkgroup air; a scenario's own air section wins; one radio
    has none. The air part accepts the default for 8 radios."""
    from types import SimpleNamespace
    for radios in (2, 3, 8):
        air = vradio.air_settings(SimpleNamespace(scenario=None, radios=radios))
        assert air == vradio.group_air(radios) and air["groups"] == [list(range(1, radios + 1))]
        assert [(r["storage"], r["radio_index"], r["identity"]) for r in air["radios"]] == [
            ("dmr-group", k, 3100000 + k) for k in range(1, radios + 1)]
    assert vradio.air_settings(SimpleNamespace(scenario=None, radios=1)) is None
    world = stub_world(radios=8)
    install_parts(world)
    reply = world.api.dispatch({**vradio.group_air(8), "version": 1, "command": "air_config"})
    assert reply["ok"], reply
    world.start()
    args = world.radios[8].channel.args
    assert args[args.index("--storage"):args.index("--storage") + 4] == ("--storage", "dmr-group", "--radio-index", "8")
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"version": 1, "radios": 2, "air": {"groups": [[1, 2]], "seed": 7, "radios": []}}))
    assert vradio.air_settings(SimpleNamespace(scenario=str(path), radios=2))["seed"] == 7
