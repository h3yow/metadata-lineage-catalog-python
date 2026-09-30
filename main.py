"""元数据血缘目录服务 HTTP 入口。

核心逻辑全部在 metadata_catalog 包内，这里只做参数适配。
服务实例是进程内单例，适合演示与单测；持久化不在本任务范围内。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from metadata_catalog.errors import (
    EntityNotFoundError,
    RejectedError,
    RejectionReason,
)
from metadata_catalog.models import NodeRef
from metadata_catalog.service import MetadataCatalogService
from metadata_catalog.store import DatasetReport

app = FastAPI(title="Metadata Lineage Catalog", version="0.1.0")
service = MetadataCatalogService()


class FieldIn(BaseModel):
    name: str | None = None
    type: str | None = None
    description: str | None = None


class DatasetIn(BaseModel):
    name: str
    owner: str | None = None
    owners: list[str] | None = None
    tags: list[str] | None = None
    description: str | None = None
    fields: list[FieldIn] | None = None
    actor: str = "anonymous"
    source: str = "unknown"


class LineageIn(BaseModel):
    upstream: str = Field(description="上游数据集名")
    downstream: str = Field(description="下游数据集名")
    actor: str = "anonymous"
    source: str = "unknown"


class FieldLineageIn(BaseModel):
    upstream_dataset: str
    upstream_field: str
    downstream_dataset: str
    downstream_field: str
    actor: str = "anonymous"
    source: str = "unknown"


@app.exception_handler(RejectedError)
def _rejected(_request: Any, exc: RejectedError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"rejections": [exc.rejection.to_dict()]})


@app.exception_handler(EntityNotFoundError)
def _not_found(_request: Any, exc: EntityNotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "rejections": [{
                "reason": RejectionReason.ENTITY_NOT_FOUND.value,
                "message": str(exc),
                "subject": exc.entity_id,
                "details": {"entity_kind": exc.entity_kind},
            }]
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/datasets", status_code=200)
def register_dataset(payload: DatasetIn) -> dict[str, object]:
    report = DatasetReport(
        name=payload.name,
        owner=payload.owner,
        owners=payload.owners,
        tags=payload.tags,
        description=payload.description,
        fields=[f.model_dump(exclude_none=True) for f in payload.fields or []],
        actor=payload.actor,
        source=payload.source,
    )
    result = service.register_dataset(report)
    return {
        "dataset": result.dataset.to_dict(),
        "created": result.created,
        "added_fields": result.added_fields,
        "merged_fields": result.merged_fields,
        "rejections": [r.to_dict() for r in result.rejections],
    }


@app.get("/datasets/{name}")
def get_dataset(name: str) -> dict[str, object]:
    return service.get_dataset(name).to_dict()


@app.get("/datasets")
def search_datasets(
    name_contains: str | None = None,
    tag: list[str] | None = Query(default=None),  # noqa: B008  # FastAPI 惯用法
    owner: list[str] | None = Query(default=None),  # noqa: B008
    source: str | None = None,
) -> dict[str, object]:
    datasets = service.search_datasets(
        name_contains=name_contains, tags=tag, owners=owner, source=source
    )
    return {"count": len(datasets), "datasets": [d.to_dict() for d in datasets]}


@app.post("/lineage/datasets", status_code=200)
def add_dataset_lineage(payload: LineageIn) -> dict[str, object]:
    result = service.add_dataset_lineage(
        payload.upstream, payload.downstream, actor=payload.actor, source=payload.source
    )
    body: dict[str, object] = {
        "ok": result.ok,
        "created": result.created,
        "activated": result.activated,
        "edge": result.edge.to_dict() if result.edge else None,
    }
    if result.rejection is not None:
        body["rejection"] = result.rejection.to_dict()
    return JSONResponse(status_code=200 if result.ok else 409, content=body)


@app.post("/lineage/fields", status_code=200)
def add_field_lineage(payload: FieldLineageIn) -> dict[str, object]:
    result = service.add_field_lineage(
        payload.upstream_dataset, payload.upstream_field,
        payload.downstream_dataset, payload.downstream_field,
        actor=payload.actor, source=payload.source,
    )
    body: dict[str, object] = {
        "ok": result.ok,
        "created": result.created,
        "activated": result.activated,
        "edge": result.edge.to_dict() if result.edge else None,
    }
    if result.rejection is not None:
        body["rejection"] = result.rejection.to_dict()
    return JSONResponse(status_code=200 if result.ok else 409, content=body)


def _expand(nodes: list) -> list[dict[str, object]]:
    return [n.to_dict() for n in nodes]


@app.get("/lineage/datasets/{name}/upstream")
def dataset_upstream(name: str, max_depth: int | None = None) -> dict[str, object]:
    service.get_dataset(name)
    return {"node": NodeRef.for_dataset(name).node_id, "upstream": _expand(service.upstream_datasets(name, max_depth=max_depth))}


@app.get("/lineage/datasets/{name}/downstream")
def dataset_downstream(name: str, max_depth: int | None = None) -> dict[str, object]:
    service.get_dataset(name)
    return {"node": NodeRef.for_dataset(name).node_id, "downstream": _expand(service.downstream_datasets(name, max_depth=max_depth))}


@app.get("/lineage/edges")
def list_edges(include_dangling: bool = True) -> dict[str, object]:
    edges = service.all_edges(include_dangling=include_dangling)
    return {"count": len(edges), "edges": [e.to_dict() for e in edges]}


@app.get("/history/dataset/{name}")
def dataset_history(name: str) -> dict[str, object]:
    return {"target": f"dataset:{name}", "entries": [e.to_dict() for e in service.dataset_history(name)]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
