"""A credential-free API substitute: one JSON request/response per TCP connection."""

import asyncio
import base64
import json

RECORDS = [{"id": 1, "name": "local fixture"}, {"id": 2, "name": "TaskForge"}]
CONTENT = b"TaskForge local backup fixture\n"


async def serve() -> None:
    failed: set[str] = set()

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(await reader.readline())
            if request["operation"] == "transient" and request["key"] not in failed:
                failed.add(request["key"])
                response = {"error": "temporary upstream failure"}
            elif request["operation"] in ("records", "transient"):
                response = {"records": RECORDS}
            elif request["operation"] == "download":
                response = {"content": base64.b64encode(CONTENT).decode("ascii")}
            else:
                response = {"error": "unknown operation"}
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    async with server:
        print(server.sockets[0].getsockname()[1], flush=True)
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(serve())
