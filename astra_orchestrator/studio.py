"""Studio view of the production route topology, without executable role calls."""

from __future__ import annotations

from typing import Any

from astra_orchestrator.graph import RuntimeState, _compile_graph


def _preview_only(state: RuntimeState) -> dict[str, Any]:
    raise RuntimeError(
        "This Studio graph displays the route topology only. "
        "Use astra_orchestrator.cli to start or resume a workflow."
    )


graph = _compile_graph(
    _preview_only,
    _preview_only,
    _preview_only,
    _preview_only,
    _preview_only,
)
