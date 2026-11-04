"""Disposable PostgreSQL pair on local Docker, joined by a shaped link. Only t8-lag-* is ever touched."""
import json
import os
import re
import subprocess
import time
import uuid

POSTGRES_IMAGE = "public.ecr.aws/docker/library/postgres:18.4"
SHAPER_BASE = ("public.ecr.aws/docker/library/alpine:3.20"
               "@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc")
# One tag per process: each run removes its image at the end, and other runs may still be going.
SHAPER_IMAGE = f"t8-lag-shaper:{os.getpid()}"
# Assumed link conditions, not measurements of the actual Tailscale path.
CONDITIONS = {
    "unlimited": None,
    "100mbit-rtt30": {"mbit": 100, "rtt_ms": 30},
    "20mbit-rtt30": {"mbit": 20, "rtt_ms": 30},
}
# Same limits and timeouts as the actual CNPG clusters: render.py resources and CNPG 1.30.1 defaults.
DB_LIMITS = ("--cpus", "1", "--memory", "1g")
DB_PARAMETERS = {"wal_level": "logical", "wal_sender_timeout": "5s", "wal_receiver_timeout": "5s",
                 "max_slot_wal_keep_size": "1GB"}
QUEUE_SECONDS = 0.1
MTU_BYTES = 1500


def server_arguments(parameters):
    """None leaves a setting out. A command-line value outranks ALTER SYSTEM, so a trial that changes one must omit it."""
    return [item for key, value in parameters.items() if value is not None for item in ("-c", f"{key}={value}")]


def require_owned_name(name):
    if not re.fullmatch(r"t8-lag-[a-z0-9-]+", name):
        raise ValueError("refusing to touch a resource outside the t8-lag- prefix")
    return name


def queue_packets(mbit, delay_ms):
    """Delay line plus a 100 ms queue: without a bound, netem hides congestion in a huge buffer."""
    return round(mbit * 1_000_000 / 8 * (delay_ms / 1000 + QUEUE_SECONDS) / MTU_BYTES)


def require_interface(interface):
    if not re.fullmatch(r"[a-z0-9]{1,15}", interface):
        raise ValueError("unexpected interface name")
    return interface


def link_down_script(interface, loss_pct):
    """Silent loss, not a closed port: neither side is told, which is how a real path failure looks."""
    return f"tc qdisc replace dev {require_interface(interface)} root netem loss {loss_pct}%"


def link_up_script(interface, shaping):
    return shaping_script(interface, shaping) if shaping else f"tc qdisc del dev {require_interface(interface)} root"


def shaping_script(interface, shaping):
    require_interface(interface)
    delay_ms = shaping["rtt_ms"] // 2  # Applied once per direction, so the two sides add up to the RTT.
    rate = f"{shaping['mbit']}mbit"
    # Offload off so the queue limit counts real packets rather than 64 KB segments.
    return (f"ethtool -K {interface} tso off gso off && "
            f"tc qdisc replace dev {interface} root netem delay {delay_ms}ms rate {rate} "
            f"limit {queue_packets(shaping['mbit'], delay_ms)}")


def minimum_transfer_ms(wire_bytes, mbit):
    """The fastest a transfer can finish at the configured rate; a faster result means no shaping."""
    return None if mbit is None else wire_bytes * 8 / (mbit * 1_000_000) * 1000


def docker(*args, input_text=None, check=True):
    done = subprocess.run(["docker", *args], input=input_text, capture_output=True, text=True, check=False)
    if check and done.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def build_shaper():
    dockerfile = f"FROM {SHAPER_BASE}\nRUN apk add --no-cache iproute2 iputils iperf3 ethtool\n"
    docker("build", "-q", "-t", SHAPER_IMAGE, "-", input_text=dockerfile)


def remove_shaper():
    docker("image", "rm", "-f", SHAPER_IMAGE, check=False)


def other_containers(lines):
    """Docker prints -- for a container with no CPU reading yet; it still counts as running."""
    others = [line.split("\t") for line in lines if not line.startswith("t8-lag-")]
    return {"other_running_containers": len(others),
            "other_containers_cpu_pct": round(sum(float(cpu.rstrip("%")) for _, cpu in others if cpu != "--"), 1)}


def machine_load():
    """Other sessions share this machine, so its load belongs next to every result."""
    lines = docker("stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}").splitlines()
    return {"load_avg_1m": round(os.getloadavg()[0], 2), **other_containers(lines)}


class Lab:
    def __init__(self, shaping, parameters=None):
        prefix = f"t8-lag-{uuid.uuid4().hex[:8]}"
        self.shaping = shaping
        self.parameters = {**DB_PARAMETERS, **(parameters or {})}
        self.prefix = prefix
        self.control_network, self.wan_network = f"{prefix}-ctl", f"{prefix}-wan"
        self.names = {"source": f"{prefix}-src", "target": f"{prefix}-dst"}
        self.ports, self.wan = {}, {}
        self.containers, self.networks = [], []

    def __enter__(self):
        try:
            for network in (self.control_network, self.wan_network):
                docker("network", "create", require_owned_name(network))
                self.networks.append(network)
            for side, name in self.names.items():
                self._start(side, name)
            self._wait_ready()
            if self.shaping:
                for side in self.names:
                    self._sidecar(side, "sh", "-c", shaping_script(self.wan[side]["interface"], self.shaping), cap_add=True)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        for name in self.containers:
            docker("rm", "-fv", require_owned_name(name), check=False)
        for network in self.networks:
            docker("network", "rm", require_owned_name(network), check=False)

    def _start(self, side, name):
        settings = server_arguments(self.parameters)
        # Trust stays inside a throwaway container bound to loopback; no credential exists to leak.
        self.containers.append(require_owned_name(name))
        docker("run", "-d", "--name", name, "--network", self.control_network, *DB_LIMITS,
               "-p", "127.0.0.1::5432", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=chat",
               POSTGRES_IMAGE, *settings)
        docker("network", "connect", self.wan_network, name)
        self.ports[side] = docker("port", name, "5432/tcp").splitlines()[0].rsplit(":", 1)[1]
        network = json.loads(docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", name))[self.wan_network]
        interface = docker("exec", name, "sh", "-c",
                           f"grep -l {network['MacAddress']} /sys/class/net/*/address").split("/")[4]
        self.wan[side] = {"ip": network["IPAddress"], "interface": interface}

    def _wait_ready(self):
        import psycopg

        for side in self.names:
            deadline = time.monotonic() + 60
            while True:
                try:
                    psycopg.connect(self.dsn(side), connect_timeout=2).close()
                    break
                except psycopg.OperationalError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.2)

    def _sidecar(self, side, *command, cap_add=False, detach=False, suffix="shaper"):
        """Tools live beside the database in its network namespace; the DB image stays unchanged."""
        name = f"{self.prefix}-{side[:3]}-{suffix}"
        args = ["run", "--name", require_owned_name(name), "--network", f"container:{self.names[side]}"]
        args += ["--cap-add", "NET_ADMIN"] if cap_add else []
        args += ["-d"] if detach else ["--rm"]
        if detach:
            self.containers.append(name)
        return docker(*args, SHAPER_IMAGE, *command)

    def dsn(self, side, user="postgres", dbname="chat"):
        return f"host=127.0.0.1 port={self.ports[side]} dbname={dbname} user={user}"

    def _link(self, script_for):
        """Both directions, through a sidecar kept running: a new container per change would blur the timing."""
        for side in self.names:
            name = f"{self.prefix}-{side[:3]}-link"
            if name not in self.containers:
                self._sidecar(side, "sleep", "86400", cap_add=True, detach=True, suffix="link")
            docker("exec", name, "sh", "-c", script_for(self.wan[side]["interface"]))

    def link_down(self, loss_pct=100):
        self._link(lambda interface: link_down_script(interface, loss_pct))

    def link_up(self):
        self._link(lambda interface: link_up_script(interface, self.shaping))

    def log_lines(self, side):
        # The database logs to stderr, which docker() does not return.
        done = subprocess.run(["docker", "logs", self.names[side]], capture_output=True, text=True, check=True)
        return (done.stdout + done.stderr).splitlines()

    def tx_bytes(self, side):
        interface = self.wan[side]["interface"]
        return int(docker("exec", self.names[side], "cat", f"/sys/class/net/{interface}/statistics/tx_bytes"))

    def check_path(self):
        """Measure the link itself, so a shaping mistake cannot pass as a fast replication result."""
        peer = self.wan["target"]["ip"]
        ping = self._sidecar("source", "ping", "-c", "10", "-i", "0.2", "-q", peer, suffix="ping")
        rtt = float(re.search(r"= [\d.]+/([\d.]+)/", ping).group(1))
        self._sidecar("target", "iperf3", "-s", "-1", detach=True, suffix="iperf")
        time.sleep(0.5)
        result = json.loads(self._sidecar("source", "iperf3", "-c", peer, "-t", "3", "-J", suffix="iperf"))
        return {"rtt_ms": rtt, "throughput_mbit": round(result["end"]["sum_received"]["bits_per_second"] / 1e6, 1),
                "retransmits": result["end"]["sum_sent"]["retransmits"]}
