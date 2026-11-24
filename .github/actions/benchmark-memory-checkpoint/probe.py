"""Sample a production baseline in place and preserve incremental evidence."""

import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


REPORTS = Path(os.environ["REPORTS"])
STARTED = REPORTS / "started"
STATUS = REPORTS / "exit-code"
INVENTORY = Path("perf/config/benchmark-inventory.json")


def split_baseline():
    script = Path("perf/scripts/compare-model-baseline.sh")
    source = script.read_text()
    original = """  if ! go test -run '^$' -bench '^(BenchmarkClassify|BenchmarkCache)' \\
    -benchmem -benchtime="${PERF_MODEL_BENCHTIME:-3s}" -timeout=30m ./benchmarks/... \\
    2>&1 | tee "$OUTPUT_DIR/model-baseline-output.txt"; then"""
    replacement = """  run_model_groups() {
    local suite
    for suite in '^BenchmarkCache' '^BenchmarkClassify(Batch|Domain|RuntimeOverhead)' '^BenchmarkClassifyInputLength'; do
      echo "Benchmark group: $suite"
      go test -run '^$' -bench "$suite" \\
        -benchmem -benchtime="${PERF_MODEL_BENCHTIME:-3s}" -timeout=30m ./benchmarks/... || return
    done
  }
  if ! run_model_groups 2>&1 | tee "$OUTPUT_DIR/model-baseline-output.txt"; then"""
    if source.count(original) != 1:
        raise RuntimeError("Unexpected baseline script")
    script.write_text(source.replace(original, replacement))


def key_values(path):
    return dict(line.split(maxsplit=1) for line in Path(path).read_text().splitlines())


def process_tree(root):
    processes = subprocess.check_output(
        ["ps", "-e", "-ww", "-o", "pid=,ppid=,rss=,args="], text=True
    )
    children = {}
    for row in processes.splitlines():
        fields = row.split(maxsplit=3)
        if len(fields) < 4:
            continue
        pid, parent, rss = map(int, fields[:3])
        children.setdefault(parent, []).append((pid, rss, fields[3]))

    pending = [root]
    tree_rss = 0
    runtime_rss = []
    while pending:
        for pid, rss, args in children.get(pending.pop(), []):
            pending.append(pid)
            if pid == root or args.startswith("ps -e -ww "):
                continue
            tree_rss += rss
            if " -m vllm_srun " in args and not args.startswith(("/bin/sh ", "sh ")):
                runtime_rss.append((pid, rss))
    return tree_rss, runtime_rss


def resource_values():
    memory = key_values("/proc/meminfo")
    vm = key_values("/proc/vmstat")
    pressure = key_values("/proc/pressure/memory")
    return (
        int(memory["MemTotal:"].split()[0]),
        int(memory["MemAvailable:"].split()[0]),
        int(vm["oom_kill"]),
        int(pressure["some"].split("total=")[1]),
        int(pressure["full"].split("total=")[1]),
    )


def run():
    started = time.time()
    STARTED.write_text(str(started))
    exit_code = 1
    try:
        if os.environ["MODE"] == "split":
            split_baseline()
        with (REPORTS / "command.log").open("w") as command_log, (
            REPORTS / "memory.csv"
        ).open("w") as samples:
            fields = [
                "elapsed_s",
                "mem_total_kib",
                "mem_available_kib",
                "oom_kills",
                "psi_some_us",
                "psi_full_us",
                "process_tree_rss_kib",
                "runtime_count",
                "runtime_pid_rss_kib",
            ]
            writer = csv.writer(samples)
            writer.writerow(fields)
            initial = resource_values()
            writer.writerow([0, *initial, 0, 0, ""])
            samples.flush()
            command = [
                "make",
                "perf-model-baseline",
                f"PERF_BASE_REF={os.environ['BASE_SHA']}",
                f"PERF_MODEL_BASELINE_DIR={REPORTS}",
            ]
            with subprocess.Popen(
                command, stdout=command_log, stderr=subprocess.STDOUT
            ) as proc:
                while proc.poll() is None:
                    tree_rss, runtimes = process_tree(os.getpid())
                    writer.writerow(
                        [
                            round(time.time() - started, 2),
                            *resource_values(),
                            tree_rss,
                            len(runtimes),
                            " ".join(f"{pid}:{rss}" for pid, rss in runtimes),
                        ]
                    )
                    samples.flush()
                    time.sleep(1)
                exit_code = proc.returncode
    finally:
        STATUS.write_text(str(exit_code))


def checkpoint(seconds):
    while not STATUS.exists():
        if STARTED.exists() and time.time() - float(STARTED.read_text()) >= seconds:
            break
        time.sleep(1)
    output = Path(os.environ["RUNNER_TEMP"]) / "benchmark-checkpoint"
    output.mkdir(exist_ok=True)
    for name in (
        "runner.txt",
        "command.log",
        "memory.csv",
        "model-baseline-output.txt",
        "exit-code",
        "supervisor.log",
    ):
        source = REPORTS / name
        if source.exists():
            shutil.copyfile(source, output / name)
    for name in ("command.log", "memory.csv"):
        source = output / name
        if source.exists():
            print(f"--- {name} ---")
            print("\n".join(source.read_text().splitlines()[-6:]))


def verify():
    while not STATUS.exists():
        time.sleep(1)
    exit_code = int(STATUS.read_text())
    if exit_code:
        print(
            (REPORTS / "command.log").read_text()[-5000:]
            if (REPORTS / "command.log").exists()
            else ""
        )
        raise SystemExit(exit_code)
    result = json.loads((REPORTS / "model-baseline.json").read_text())
    expected = {
        name
        for name in json.loads(INVENTORY.read_text())["benchmarks"]
        if name.startswith(("BenchmarkCache", "BenchmarkClassify"))
    }
    measured = set(result["benchmarks"])
    if measured != expected:
        raise RuntimeError(
            f"Missing {sorted(expected - measured)}; unexpected {sorted(measured - expected)}"
        )
    print(f"Measured and verified all {len(measured)} Cache/Classify benchmarks")


if __name__ == "__main__":
    if sys.argv[1] == "checkpoint":
        checkpoint(int(sys.argv[2]))
    else:
        {"run": run, "verify": verify}[sys.argv[1]]()
