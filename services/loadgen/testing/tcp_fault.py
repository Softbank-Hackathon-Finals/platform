#!/usr/bin/env python3
"""TCP 결함 중계기. 로컬 시험 전용이다.

    tcp_fault.py <듣는 포트> <대상 포트> [--rtt-ms N] [--drop-every K] [--drop-chance P]

--rtt-ms: 왕복 지연을 넣는다. 클라우드를 넘는 쓰기(설계 8.6)의 실측값이 아직 없어 영향만 미리 본다.
--drop-every: 대상이 보낸 응답을 K번째마다 버리고 연결을 끊는다. 서버는 이미 처리한 뒤다(응답 유실).
--drop-chance: 응답마다 확률 P로 같은 일을 한다. 규칙적으로 끊으면 늘 같은 문장에서만 끊길 수 있다. 시드는 고정이다.
"""

import argparse
import asyncio
import itertools
import random


async def _forward(reader, writer, one_way_s, should_drop=lambda: False):
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    async def send_later():
        while True:
            due, data = await queue.get()
            delay = due - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if data is None:
                writer.close()
                return
            writer.write(data)
            await writer.drain()

    sender = asyncio.create_task(send_later())
    try:
        while data := await reader.read(65536):
            if should_drop():
                writer.transport.abort()
                break
            queue.put_nowait((loop.time() + one_way_s, data))
    except ConnectionError:
        pass
    queue.put_nowait((loop.time() + one_way_s, None))
    await sender


async def main(args: argparse.Namespace) -> None:
    one_way_s = args.rtt_ms / 2000
    responses = itertools.count(1)
    chance = random.Random(1)

    def should_drop() -> bool:
        if args.drop_chance > 0:
            return chance.random() < args.drop_chance
        return args.drop_every > 0 and next(responses) % args.drop_every == 0

    async def handle(reader, writer):
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", args.target_port)
        await asyncio.gather(
            _forward(reader, upstream_writer, one_way_s),
            _forward(upstream_reader, writer, one_way_s, should_drop),
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
    parser.add_argument("--rtt-ms", type=float, default=0)
    parser.add_argument("--drop-every", type=int, default=0)
    parser.add_argument("--drop-chance", type=float, default=0)
    asyncio.run(main(parser.parse_args()))
