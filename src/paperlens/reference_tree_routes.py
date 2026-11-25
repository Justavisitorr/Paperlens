"""Workspace-scoped routing for lazy, public citation-tree lookups."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request

from .reference_tree import ReferenceTreeService, reference_text


def register_reference_tree_routes(
    app: FastAPI,
    *,
    paper_loader,
    cache_dir: Path | Callable[[], Path],
    allow_network: Callable[[], bool] = lambda: True,
    service: ReferenceTreeService | None = None,
) -> None:
    service = service or ReferenceTreeService(cache_dir, allow_network=allow_network)

    @app.get("/api/library/{paper_id}/references/{reference_id}/tree")
    def reference_tree(
        request: Request,
        paper_id: str,
        reference_id: str,
        node: Annotated[str, Query(max_length=17, pattern=r"^(W[1-9][0-9]{0,15})?$")] = "",
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
        refresh: bool = False,
    ) -> dict:
        # Load through the existing account-local library before any cache/network access.
        report = paper_loader(request, paper_id)
        item = (
            next((item for item in report.reading.references if item.id == reference_id), None)
            if report.reading
            else None
        )
        if item is None:
            raise HTTPException(404, "这篇论文中没有该参考文献。")
        try:
            return service.branch(
                reference_text(report, item), item.href, node=node, offset=offset, refresh=refresh
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
