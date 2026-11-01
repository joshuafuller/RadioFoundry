# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""tools/demo.py without radios: arguments, the plan of each mode, the summary table and each check's verdict."""

from pathlib import Path

import pytest

from tools import demo


def test_default_is_full_mode_into_build_demo():
    args = demo.arguments([])
    assert args.mode == "full" and args.out == (demo.ROOT / "build/demo").resolve()
    assert args.serve_world is None


def test_fast_mode_and_out_folder(tmp_path):
    args = demo.arguments(["--fast", "--out", str(tmp_path / "run")])
    assert args.mode == "fast" and args.out == (tmp_path / "run").resolve()


@pytest.mark.parametrize("options", [["--speed"], ["--fast", "extra"], ["--out"]])
def test_bad_arguments(options, capsys):
    with pytest.raises(SystemExit) as caught:
        demo.arguments(options)
    assert caught.value.code == 2
    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1 and error.startswith("demo: ") and "--help" in error


def test_out_must_be_a_folder(tmp_path, capsys):
    file = tmp_path / "file"
    file.write_text("x")
    with pytest.raises(SystemExit) as caught:
        demo.arguments(["--out", str(file)])
    assert caught.value.code == 2 and "folder" in capsys.readouterr().err


def test_serve_world_is_hidden_from_help(capsys):
    with pytest.raises(SystemExit):
        demo.arguments(["--help"])
    text = capsys.readouterr().out
    assert "[--fast]" in text and "[--serve-world" not in text and "--serve-until-ms" not in text


def names(args):
    return [name for name, _, _ in demo.plan(args)]


def test_plan_order_follows_the_issue(tmp_path):
    args = demo.arguments(["--fast", "--fixed", str(tmp_path / "missing.elf")])
    assert names(args) == ["launcher", "world", "group call", "collision", "loss fault", "sms", "record",
                           "time travel", "interaction", "parrot echo", "sms after reboot", "scale"]
    assert all(narration.strip() and "\n" not in narration for _, narration, _ in demo.plan(args))


def test_full_plan_adds_parrot_replay_and_mixed_with_the_fixed_build(tmp_path):
    fixed = tmp_path / "fixed.elf"
    fixed.write_text("")
    steps = names(demo.arguments(["--fixed", str(fixed)]))
    assert steps.index("parrot echo") < steps.index("parrot replay") < steps.index("parrot mixed")
    assert "parrot mixed" not in names(demo.arguments(["--fixed", str(tmp_path / "missing.elf")]))


def test_timeline_keys_are_inside_the_world_and_travel_is_inside_the_call():
    assert all(demo.T["ready"] < at < demo.T["end"] for _, at, _ in demo.KEYS)
    assert demo.T["call_down"] < demo.TRAVEL_MS * 1000 < demo.T["call_up"]
    assert demo.T["collide_up"] < demo.T["sms_send"] - 500_000
    assert all(demo.LOSS_T["ready"] < at < demo.LOSS_T["end"] for _, at, _ in demo.LOSS_KEYS)
    assert demo.LOSS_T["down"] < demo.LOSS_T["start"] + 300_000 < demo.LOSS_T["up"]   # console lead 300 ms


def test_table_has_every_step_and_a_total():
    steps = [demo.Step("group call", "PASS", "radio 2 shows call from 3100001", 2.5),
             demo.Step("collision", "FAIL", "0 collision decisions", 3.04)]
    lines = demo.table(steps).splitlines()
    assert lines[0].split() == ["step", "result", "evidence", "wall", "s"]
    assert set(lines[1].replace(" ", "")) == {"-"}
    assert lines[2].startswith("group call") and "PASS" in lines[2] and lines[2].endswith("2.5")
    assert "FAIL" in lines[3] and lines[3].endswith("3.0")
    assert lines[-1] == "1 of 2 steps passed in 5.5 s"
    assert len({line.index("PASS") for line in lines[2:3]} | {lines[3].index("FAIL")}) == 1   # aligned columns


def test_exit_code():
    assert demo.exit_code([demo.Step("a", "PASS"), demo.Step("b", "SKIP")]) == 0
    assert demo.exit_code([demo.Step("a", "PASS"), demo.Step("b", "FAIL")]) == 1
    assert demo.exit_code([]) == 1


def test_incomplete_parrot_echo_is_not_a_reproduction():
    assert demo.parrot_verdict("echo", 0, ["parrot_check: PASS --part echo"])[0] == "FAIL"
    assert demo.parrot_verdict("echo", 2, ["parrot_check: INCOMPLETE: FileNotFoundError: x"]) == (
        "FAIL", "exit 2: parrot_check: INCOMPLETE: FileNotFoundError: x")


def test_parrot_replay_and_mixed_must_pass():
    assert demo.parrot_verdict("mixed", 0, ["parrot_check: PASS --part mixed"])[0] == "PASS"
    assert demo.parrot_verdict("replay", 0, ["parrot_check: PASS --part replay"])[0] == "PASS"
    assert demo.parrot_verdict("mixed", 1, ["mixed FAIL x"])[0] == "FAIL"


def test_sms_reboot_verdicts():
    lines = ["[hypothesis]   B (radio 2): inbox_count=1 sms_state=idle sms_received=1 sms_duplicates=1",
             "sms_restart_check: CONFIRMED"]
    assert demo.sms_reboot_verdict(0, lines) == (
        "PASS", "bug reproduced (CONFIRMED): after the reboot B received=1 duplicates=1")
    assert demo.sms_reboot_verdict(0, lines[:1] + ["sms_restart_check: REFUTED"])[0] == "FAIL"
    assert demo.sms_reboot_verdict(2, ["sms_restart_check: UNTESTABLE: x"]) == (
        "FAIL", "exit 2: sms_restart_check: UNTESTABLE: x")


def test_interaction_verdict():
    lines = ["SC1 at 1900000 us radio 2 view: call.src=3100001 call.dst=3100002 call.group=0 dmr.state=rx",
             "SC1 at 4400000 us radio 2 view: data.sms_received=1; radio 1 view: data.sms_acked=1",
             "interaction_check: PASS --part air"]
    assert demo.interaction_verdict(0, lines) == ("PASS", "radio 2 shows call from 3100001, SMS received and ACKed")
    assert demo.interaction_verdict(1, lines[:1] + ["SC1 FAIL x"]) == ("FAIL", "exit 1: SC1 FAIL x")


def test_scale_verdict():
    lines = ["    N  ok  start s    run s  v/wall  peak RSS MiB",
             "    4 yes      0.2      2.2    0.90            42      2.8        28     2.3      0.5   3.0   3.9",
             "leftover processes of this executable: 0"]
    assert demo.scale_verdict(0, lines) == ("PASS", "4 radios ok at v/wall 0.90, 0 left over")
    assert demo.scale_verdict(0, lines[:2] + ["leftover processes of this executable: 3"])[0] == "FAIL"


def test_radio_processes_matches_the_exact_executable(tmp_path):
    assert demo.radio_processes([tmp_path / "no-such.elf"]) == []
    import sys
    assert __import__("os").getpid() in demo.radio_processes([Path(sys.executable)])


def test_resumed_demo_segment_reacquires_keys(stub_world, tmp_path):
    from tools.conductor.parts.input import install
    world = stub_world()
    install(world)
    world.start()
    show = demo.Showcase(tmp_path, tmp_path, {})

    class Client:
        def call(self, command, **fields):
            reply = world.api.dispatch({"version": 1, "command": command, **fields})
            assert reply["ok"], reply
            return reply["result"]

    show.client = Client()
    show.keys = [(1, 1000, "down"), (1, 4000, "up")]
    show.start_run(until_us=500)
    while world.state != "Paused":
        world.advance()
    accepted = []
    world.on_input(accepted.append)
    show.start_run(until_us=5000)
    assert [(event.time_us, event.payload) for event in accepted] == [(1000, b"\x04\x01"), (4000, b"\x04\x00")]
