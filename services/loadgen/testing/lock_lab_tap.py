#!/usr/bin/env python3
"""앱과 Postgres 사이에서 프로토콜 메시지를 읽어 기록하는 중계기. 로컬 시험 전용이다.

    lock_lab_tap.py <듣는 포트> <대상 포트> <기록 파일> [--rtt-ms N]

왕복 수를 코드에서 짐작하지 않고 실제 오간 메시지로 센다. 기록의 t는 지연을 지나 상대에게 넘긴 시각이다.
앱 → DB 메시지의 t는 DB가 받은 시각에 가깝다. 그래서 잠금 문장과 커밋 사이 간격이 잠금 점유 시간이 된다.
SSL 협상은 해석하지 않으므로 sslmode=disable로 붙는다. 내용은 SQL 앞부분만 남기고 값은 남기지 않는다.
"""

import argparse
import asyncio
import itertools
import json
import struct
import time

# Parse는 (이름, SQL), Query는 (SQL)이 본문이다. 다른 메시지는 종류만 남긴다.
FRONTEND_NAMES = {
    b"Q": "Query", b"P": "Parse", b"B": "Bind", b"E": "Execute", b"S": "Sync", b"D": "Describe",
    b"C": "Close", b"H": "Flush", b"X": "Terminate",
}  # fmt: skip
BACKEND_NAMES = {b"Z": "ReadyForQuery", b"C": "CommandComplete", b"E": "Error"}


def split_messages(buffer: bytearray, *, startup: bool) -> list[tuple[bytes, bytes]]:
    """버퍼에서 온전한 메시지만 떼어 낸다. 시작 패킷은 종류 바이트가 없다."""
    messages = []
    if startup:
        if len(buffer) < 4:
            return messages
        length = struct.unpack("!I", buffer[:4])[0]
        if len(buffer) < length:
            return messages
        del buffer[:length]
        messages.append((b"startup", b""))
    while len(buffer) >= 5:
        length = struct.unpack("!I", buffer[1:5])[0]
        if len(buffer) < 1 + length:
            break
        messages.append((bytes(buffer[:1]), bytes(buffer[5 : 1 + length])))
        del buffer[: 1 + length]
    return messages


def describe(kind: bytes, payload: bytes, *, frontend: bool) -> dict | None:
    names = FRONTEND_NAMES if frontend else BACKEND_NAMES
    if kind not in names:
        return None
    entry: dict = {"m": names[kind]}
    if frontend and kind == b"Q":
        entry["sql"] = payload.split(b"\0", 1)[0].decode(errors="replace").strip()[:300]
    elif frontend and kind == b"P":
        entry["sql"] = payload.split(b"\0", 2)[1].decode(errors="replace").strip()[:300]
    elif not frontend and kind == b"Z":
        entry["status"] = payload.decode()
    elif not frontend and kind == b"E":
        # 필드 C가 SQLSTATE다. 메시지 본문은 값을 담을 수 있어 남기지 않는다.
        fields = [f for f in payload.split(b"\0") if f]
        entry["sqlstate"] = next((f[1:].decode() for f in fields if f[:1] == b"C"), None)
    return entry


async def forward(reader, writer, one_way_s, direction, connection, log, *, frontend):
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    buffer = bytearray()
    startup = frontend

    async def send_later():
        while True:
            due, data, entries = await queue.get()
            delay = due - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if data is None:
                writer.close()
                return
            writer.write(data)
            await writer.drain()
            if entries:
                now = time.monotonic()
                log(json.dumps({"t": round(now * 1000, 3), "c": connection, "d": direction, "ms": entries}))

    sender = asyncio.create_task(send_later())
    try:
        while data := await reader.read(65536):
            buffer += data
            entries = []
            for kind, payload in split_messages(buffer, startup=startup):
                startup = False
                entry = describe(kind, payload, frontend=frontend)
                if entry:
                    entries.append(entry)
            queue.put_nowait((loop.time() + one_way_s, data, entries))
    except ConnectionError:
        pass
    queue.put_nowait((loop.time() + one_way_s, None, None))
    await sender


async def main(args: argparse.Namespace) -> None:
    one_way_s = args.rtt_ms / 2000
    connections = itertools.count(1)
    with open(args.log, "a", buffering=1) as output:

        def log(line: str) -> None:
            output.write(line + "\n")

        async def handle(reader, writer):
            connection = next(connections)
            upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", args.target_port)
            await asyncio.gather(
                forward(reader, upstream_writer, one_way_s, "app>db", connection, log, frontend=True),
                forward(upstream_reader, writer, one_way_s, "db>app", connection, log, frontend=False),
                return_exceptions=True,
            )
            upstream_writer.close()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", args.listen_port)
        async with server:
            await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("listen_port", type=int)
    parser.add_argument("target_port", type=int)
    parser.add_argument("log")
    parser.add_argument("--rtt-ms", type=float, default=0)
    asyncio.run(main(parser.parse_args()))
