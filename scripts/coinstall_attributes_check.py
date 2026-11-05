"""Co-install task: aihydro-tools[attributes] puts the attribute tools on the server.

Run by the ``with-attributes`` job of ``.github/workflows/install-alone.yml``
after installing the built wheel with the ``attributes`` extra (so aihydro-lsh
and aihydro-watershed come from PyPI and must resolve together with tools).
Every socket connection is refused.

  1. aihydro-lsh is importable and its watershed pin is satisfied by the
     installed aihydro-watershed (the lsh <-> tools conflict of audit C1);
  2. the MCP server lists the ``lsh_*`` tools, registered through the
     ``aihydro.tools`` entry point;
  3. ``lsh_attribute_info(p_mean)`` answers from lsh's bundled registry.

Socket block: an IN-PROCESS patch of the Python ``socket`` module (connect, create_connection,
getaddrinfo). It does not cover child processes or C libraries that open sockets themselves, so
it is evidence that this Python code made no network call, not an OS-level sandbox.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
from importlib.metadata import requires, version

from packaging.requirements import Requirement


def _block_network() -> None:
    def refuse(*_a, **_k):
        raise RuntimeError("network access attempted during an install-alone check")

    socket.socket.connect = refuse  # type: ignore[assignment]
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.getaddrinfo = refuse  # type: ignore[assignment]


async def _drive() -> tuple[set[str], dict]:
    from fastmcp import Client

    from ai_hydro.mcp import mcp

    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
        res = await client.call_tool("lsh_attribute_info", {"attribute_name": "p_mean"},
                                     raise_on_error=False)
        assert not res.is_error, res.content
        return names, json.loads(res.content[0].text)


def main() -> int:
    import aihydro_lsh

    assert "site-packages" in aihydro_lsh.__file__, aihydro_lsh.__file__
    pin = next(Requirement(r) for r in requires("aihydro-lsh") if r.startswith("aihydro-watershed"))
    assert pin.specifier.contains(version("aihydro-watershed")), (str(pin), version("aihydro-watershed"))

    os.environ["AIHYDRO_HOME"] = tempfile.mkdtemp(prefix="aihydro-alone-")
    _block_network()
    names, info = asyncio.run(_drive())
    lsh_tools = sorted(n for n in names if n.startswith("lsh_"))
    assert {"lsh_attributes", "lsh_families", "lsh_attribute_info"} <= set(lsh_tools), lsh_tools
    assert info["attribute"] == "p_mean" and info["family"] == "climate", info

    print(f"aihydro-tools + aihydro-lsh {version('aihydro-lsh')} + aihydro-watershed "
          f"{version('aihydro-watershed')}: co-install OK ({len(lsh_tools)} lsh tools)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
