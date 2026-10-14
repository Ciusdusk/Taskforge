"""Read credentials into memory without propagating subprocess output."""

import asyncio
import os
from dataclasses import dataclass, field

from integrations.github.errors import AuthenticationError
from integrations.github.lifecycle import drain, protected


@dataclass(frozen=True)
class Credential:
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.token or any(
            ord(character) < 33 or ord(character) > 126 for character in self.token
        ):
            raise AuthenticationError("Invalid GitHub credential format")


async def _gh_token() -> str | None:
    # Shield spawn too: cancellation must not orphan a newly created child.
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            "gh",
            "auth",
            "token",
            "--hostname",
            "github.com",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    )
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.shield(spawn)
        async with asyncio.timeout(10):
            stdout, _ = await process.communicate()
        if process.returncode == 0:
            return stdout.decode("utf-8", errors="replace").strip() or None
    except (OSError, TimeoutError):
        return None
    finally:
        if process is None and not spawn.done():
            process = await drain(spawn)
        elif process is None and not spawn.cancelled() and spawn.exception() is None:
            process = spawn.result()
        if process is not None and process.returncode is None:
            process.kill()
            await protected(process.communicate())
    return None


async def resolve_credential() -> Credential:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            return Credential(token)
    gh_value = await protected(_gh_token(), cancel=True)
    if gh_value:
        return Credential(gh_value)
    raise AuthenticationError("No GitHub credential: set GH_TOKEN/GITHUB_TOKEN or authenticate gh")
