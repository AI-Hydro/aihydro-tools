"""Install-alone task for aihydro-tools.

Run by ``.github/workflows/install-alone.yml`` with the *venv's* interpreter,
from a directory outside the checkout, after installing ONLY the built wheel
(no extras) and its PyPI dependencies (aihydro-core, aihydro-data and
aihydro-watershed among them). Neither aihydro-lsh nor aihydro-modelling is
present, and every socket connection is refused.

Task: drive the real MCP server in-process through a ``fastmcp`` client.
  1. the server lists its built-in tools (no lsh_* tools: lsh is not installed);
  2. ``get_metric_definition(metric.nse)`` answers from the bundled knowledge registry;
  3. ``data_list_products(streamflow)`` reaches aihydro-data through the tools wheel;
  4. a tool that needs the private aihydro-modelling package returns the
     structured ``MODELLING_NOT_INSTALLED`` envelope instead of raising.

All state goes to a temporary AIHYDRO_HOME, never the real ``~/.aihydro``.

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
from importlib.util import find_spec


def _block_network() -> None:
    def refuse(*_a, **_k):
        raise RuntimeError("network access attempted during an install-alone check")

    socket.socket.connect = refuse  # type: ignore[assignment]
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.getaddrinfo = refuse  # type: ignore[assignment]


async def _drive() -> dict:
    from fastmcp import Client

    from ai_hydro.mcp import mcp

    def body(res) -> dict | list:
        return json.loads(res.content[0].text)

    out: dict = {}
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
        out["n_tools"] = len(names)
        out["has_lsh_tools"] = any(n.startswith("lsh_") for n in names)
        out["has_core_tools"] = {"data_fetch", "get_metric_definition", "describe_model_space"} <= names

        res = await client.call_tool("get_metric_definition", {"metric_id": "metric.nse"},
                                     raise_on_error=False)
        out["metric"] = (res.is_error, body(res))
        res = await client.call_tool("data_list_products", {"variable": "streamflow"},
                                     raise_on_error=False)
        out["products"] = (res.is_error, body(res))
        res = await client.call_tool("describe_model_space", {}, raise_on_error=False)
        out["modelling"] = body(res)
    return out


def main() -> int:
    import ai_hydro

    where = ai_hydro.__file__
    assert "site-packages" in where, f"imported from a source tree, not the wheel: {where}"
    assert find_spec("aihydro_lsh") is None and find_spec("aihydro_modelling") is None, \
        "a sibling that this job must not have is installed"

    os.environ["AIHYDRO_HOME"] = tempfile.mkdtemp(prefix="aihydro-alone-")
    _block_network()
    out = asyncio.run(_drive())

    assert out["n_tools"] >= 150, out["n_tools"]
    assert out["has_core_tools"] and not out["has_lsh_tools"], out
    is_err, metric = out["metric"]
    assert not is_err and metric["id"] == "metric.nse" and "formula" in metric, metric
    is_err, products = out["products"]
    assert not is_err and "NWIS_STREAMFLOW" in {p["id"] for p in products}, products
    assert out["modelling"].get("code") == "MODELLING_NOT_INSTALLED", out["modelling"]

    print(f"aihydro-tools {ai_hydro.__version__}: install-alone task OK "
          f"({out['n_tools']} tools, {where})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
