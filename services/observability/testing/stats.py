"""k3s 노드 안 컨테이너 자원 사용량 표본. `crictl stats -o json`을 주기적으로 읽어 컨테이너별 최대 메모리 · 평균 CPU를 낸다.

메모리는 두 가지를 낸다. working set에는 커널이 OOM 전에 회수하는 파일 캐시(디스크 큐 · TSDB)가 섞이므로,
limit을 정할 때는 cgroup memory.stat의 anon(프로세스가 실제로 쥔 메모리)을 함께 본다.

사용: python3 stats.py <k3s 컨테이너 이름> <초> <간격초> 컨테이너이름...
"""

import json
import subprocess
import sys
import time

node, seconds, interval, names = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), set(sys.argv[4:])


def node_exec(*cmd: str) -> str:
    return subprocess.run(["docker", "exec", node, *cmd], capture_output=True, text=True, check=True).stdout


def anon_bytes(container_id: str) -> int:
    pid = node_exec("crictl", "inspect", "-o", "go-template", "--template", "{{.info.pid}}", container_id).strip()
    cgroup = node_exec("cat", f"/proc/{pid}/cgroup").strip().removeprefix("0::")
    stat = node_exec("cat", f"/sys/fs/cgroup{cgroup}/memory.stat")
    return next(int(line.split()[1]) for line in stat.splitlines() if line.startswith("anon "))


def sample():
    rows = {}
    for s in json.loads(node_exec("crictl", "stats", "-o", "json"))["stats"]:
        name = s["attributes"]["labels"].get("io.kubernetes.container.name")
        if name in names:
            rows[name] = (
                int(s["memory"]["workingSetBytes"]["value"]),
                int(s["cpu"]["usageCoreNanoSeconds"]["value"]),
                time.monotonic(),
                anon_bytes(s["attributes"]["id"]),
            )
    return rows


first = prev = sample()
peak = {n: 0 for n in names}
anon_peak = {n: 0 for n in names}
cpu_peak = {n: 0.0 for n in names}
end = time.monotonic() + seconds
while time.monotonic() < end:
    time.sleep(interval)
    cur = sample()
    for n, (mem, cpu, t, anon) in cur.items():
        peak[n] = max(peak[n], mem)
        anon_peak[n] = max(anon_peak[n], anon)
        if n in prev:
            cpu_peak[n] = max(cpu_peak[n], (cpu - prev[n][1]) / 1e9 / (t - prev[n][2]))
    prev = cur
for n in sorted(names):
    if n in first and n in prev:
        avg = (prev[n][1] - first[n][1]) / 1e9 / (prev[n][2] - first[n][2])
        print(
            f"  {n:12s} 최대 메모리 {peak[n] / 2**20:6.1f}MiB (anon {anon_peak[n] / 2**20:6.1f}MiB)"
            f"  평균 CPU {avg * 1000:6.1f}m  최대 CPU(간격) {cpu_peak[n] * 1000:6.1f}m"
        )
    else:
        print(f"  {n:12s} 표본 없음")
