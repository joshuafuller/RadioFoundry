# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Bounded event/choice deletion preserving a failure against a passing reference build."""
import copy
import json

from scenario import execute, integer, validate, validate_v2


def failures(result):
    if result["execution_error"] is not None:
        return None
    return [index for index, assertion in enumerate(result["assertions"]) if not assertion["passed"]]


def remove_event(value, index):
    """Delete an input and its transitive dependents; retain legal boot schedule choices."""
    candidate = copy.deepcopy(value)
    removed, kept, identities = [], [], set()
    for position, event in enumerate(candidate["events"]):
        if position == index or any(dep in identities for dep in event.get("after", [])):
            removed.append(event)
            if "id" in event:
                identities.add(event["id"])
        else:
            kept.append(event)
    candidate["events"] = kept
    return candidate, removed


def removals(value):
    for index in range(len(value["events"])):
        yield "events", index, None
    for index, radio in enumerate(value["radios"]):
        for key in ("task_schedule", "hotspot_schedule"):
            if key in radio:
                yield "radios", index, key
    for index, event in enumerate(value["events"]):
        if "phase" in event:
            yield "events", index, "phase"


def minimize(source, worker, reference, output, max_attempts):
    from tools.conductor.parts.resolve import private_folder, write_private
    integer(max_attempts, 1, 1000, "max_attempts")
    output = output.resolve()
    if output.exists():
        raise ValueError("output directory already exists; choose a new run directory")
    private_folder(output)
    initial = execute(source, worker, output / "initial-failing")
    passing = execute(source, reference, output / "initial-reference")
    signature = failures(initial)
    if not signature or passing["status"] != "passed":
        raise ValueError("reduction requires assertion failure on the worker and a passing reference")
    current = initial["scenario"]
    inputs = output / "inputs"
    private_folder(inputs)
    attempts, index, trials = 0, 0, []
    while index < len(options := list(removals(current))) and attempts < max_attempts:
        collection, position, key = options[index]
        if key is None:
            candidate, removed_events = remove_event(current, position)
            trial = {"removed": removed_events[0], "removed_dependents": removed_events[1:]}
        else:
            candidate = copy.deepcopy(current)
            removed = candidate[collection][position].pop(key)
            trial = {"removed_choice": {"collection": collection, "index": position,
                                        "key": key, "value": removed}}
        attempts += 1
        trial.update(attempt=attempts, accepted=False)
        trials.append(trial)
        path = inputs / f"{attempts:04d}.json"
        write_private(path, (json.dumps(candidate, indent=2) + "\n").encode("utf-8"))
        try:
            (validate_v2 if candidate.get("schema_version") == 2 else validate)(candidate)
        except ValueError as error:
            trial["invalid"] = str(error)
            index += 1
            continue
        bad = execute(str(path), worker, output / f"trial-{attempts:04d}-failing",
                      initial["worker_sha256"], initial["simulator_sha256"])
        if failures(bad) == signature:
            good = execute(str(path), reference, output / f"trial-{attempts:04d}-reference",
                           passing["worker_sha256"], passing["simulator_sha256"])
            if good["status"] == "passed":
                trial["accepted"] = True
                current = candidate
                index = 0
                continue
        index += 1
    path = output / "minimized.json"
    write_private(path, (json.dumps(current, indent=2) + "\n").encode("utf-8"))
    final_bad = execute(str(path), worker, output / "final-failing", initial["worker_sha256"], initial["simulator_sha256"])
    final_good = execute(str(path), reference, output / "final-reference", passing["worker_sha256"], passing["simulator_sha256"])
    if failures(final_bad) != signature or final_good["status"] != "passed":
        raise ValueError("final reduced case did not preserve the failure and passing reference")
    minimal = index == len(list(removals(current)))
    report = {"schema_version": 1, "status": "passed" if minimal else "not_run", "one_event_minimal": index >= len(current["events"]),
              "one_choice_minimal": minimal,
              "attempts": attempts, "max_attempts": max_attempts, "failure_assertions": signature,
              "original_events": len(initial["scenario"]["events"]), "remaining_events": len(current["events"]),
              "trials": trials}
    write_private(output / "result.json", (json.dumps(report, indent=2) + "\n").encode("utf-8"))
    return report
