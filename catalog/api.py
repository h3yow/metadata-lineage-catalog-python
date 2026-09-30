"""FastAPI 适配层：把领域错误转成结构化 HTTP 响应。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from catalog.errors import CatalogError
from catalog.models import EntityKind, EntityRef, FieldInfo
from catalog.search import SearchCriteria
from catalog.service import CatalogService


# --------------------------------------------------------------------- #
# 请求 / 响应模型
# --------------------------------------------------------------------- #
class RegisterDatasetRequest(BaseModel):
    name: str
    owner: str
    source: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    actor: str | None = None


class FieldPayload(BaseModel):
    name: str
    data_type: str
    source: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class UpdateDatasetRequest(BaseModel):
    source: str
    owner: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    fields_snapshot: list[FieldPayload] | None = None
    actor: str | None = None


class AddFieldRequest(BaseModel):
    dataset: str
    field: FieldPayload
    actor: str | None = None


class AddEdgeRequest(BaseModel):
    upstream_kind: EntityKind
    upstream_name: str
    downstream_kind: EntityKind
    downstream_name: str
    source: str
    actor: str | None = None


class SearchRequest(BaseModel):
    name_contains: str | None = None
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    field_name_contains: str | None = None


# --------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------- #
def _field_out(f: FieldInfo) -> dict[str, Any]:
    return {
        "name": f.name,
        "data_type": f.data_type,
        "source": f.source,
        "sources": sorted(f.sources),
        "description": f.description,
        "tags": sorted(f.tags),
        "registered_at": f.registered_at.isoformat(),
        "updated_at": f.updated_at.isoformat(),
    }


def _dataset_out(ds) -> dict[str, Any]:
    return {
        "name": ds.name,
        "owner": ds.owner,
        "source": ds.source,
        "sources": sorted(ds.sources),
        "description": ds.description,
        "tags": sorted(ds.tags),
        "fields": [_field_out(ds.fields[k]) for k in sorted(ds.fields)],
        "registered_at": ds.registered_at.isoformat(),
        "updated_at": ds.updated_at.isoformat(),
    }


def create_app(service: CatalogService | None = None) -> FastAPI:
    """构造 FastAPI 应用；可注入 service（便于测试隔离）。"""
    svc = service or CatalogService()
    app = FastAPI(title="Metadata Lineage Catalog", version="0.1.0")

    @app.exception_handler(CatalogError)
    async def _catalog_error_handler(_request, exc: CatalogError):
        return JSONResponse(
            status_code=422,
            content={
                "error": exc.reason.value,
                "message": exc.message,
                "details": exc.details,
            },
        )

    # ---------------- 数据集 / 字段 ---------------- #
    @app.post("/datasets", status_code=201)
    def register_datasets(req: RegisterDatasetRequest):
        ds, created = svc.entities.register_dataset(
            name=req.name,
            owner=req.owner,
            source=req.source,
            description=req.description,
            tags=frozenset(req.tags),
            actor=req.actor,
        )
        return {"created": created, "dataset": _dataset_out(ds)}

    @app.patch("/datasets/{name}")
    def update_datasets(name: str, req: UpdateDatasetRequest):
        snapshot = None
        if req.fields_snapshot is not None:
            snapshot = [
                FieldInfo(
                    name=p.name,
                    data_type=p.data_type,
                    source=p.source,
                    description=p.description,
                    tags=frozenset(p.tags),
                )
                for p in req.fields_snapshot
            ]
        ds = svc.entities.update_dataset(
            name=name,
            source=req.source,
            owner=req.owner,
            description=req.description,
            tags=frozenset(req.tags) if req.tags is not None else None,
            fields_snapshot=snapshot,
            actor=req.actor,
        )
        return {"dataset": _dataset_out(ds)}

    @app.get("/datasets")
    def list_datasets():
        return {"datasets": [_dataset_out(d) for d in svc.entities.all_datasets()]}

    @app.get("/datasets/{name}")
    def get_dataset(name: str):
        return _dataset_out(svc.entities.get_dataset(name))

    @app.post("/fields", status_code=201)
    def add_fields(req: AddFieldRequest):
        p = req.field
        f, created = svc.entities.add_field(
            dataset=req.dataset,
            field=FieldInfo(
                name=p.name,
                data_type=p.data_type,
                source=p.source,
                description=p.description,
                tags=frozenset(p.tags),
            ),
            actor=req.actor,
        )
        return {"created": created, "field": _field_out(f)}

    # ---------------- 血缘 ---------------- #
    @app.post("/lineage/edges", status_code=201)
    def add_edges(req: AddEdgeRequest):
        edge = svc.lineage.add_edge(
            upstream=EntityRef(kind=req.upstream_kind, name=req.upstream_name),
            downstream=EntityRef(kind=req.downstream_kind, name=req.downstream_name),
            source=req.source,
            actor=req.actor,
        )
        return {
            "upstream": {"kind": edge.upstream.kind.value, "name": edge.upstream.name},
            "downstream": {"kind": edge.downstream.kind.value, "name": edge.downstream.name},
            "source": edge.source,
            "flags": sorted(f.value for f in edge.flags),
        }

    @app.post("/lineage/refresh-dangling")
    def refresh_dangling():
        affected = svc.lineage.mark_dangling_for_missing()
        return {"marked_dangling": [f"{e.upstream.name}->{e.downstream.name}" for e in affected]}

    @app.get("/lineage/edges")
    def list_edges():
        return {
            "edges": [
                {
                    "upstream": {"kind": e.upstream.kind.value, "name": e.upstream.name},
                    "downstream": {"kind": e.downstream.kind.value, "name": e.downstream.name},
                    "source": e.source,
                    "flags": sorted(fl.value for fl in e.flags),
                }
                for e in svc.lineage.edges()
            ]
        }

    @app.get("/lineage/{kind}/{name}/{direction}")
    def lineage_query(kind: EntityKind, name: str, direction: str, max_depth: int = 50):
        ref = EntityRef(kind=kind, name=name)
        rows = svc.lineage_between(ref, direction, max_depth=max_depth)
        return {
            "ref": {"kind": kind.value, "name": name},
            "direction": direction,
            "results": [
                {
                    "kind": r.kind.value,
                    "name": r.name,
                    "distance": dist,
                    "flags": sorted(fl.value for fl in flags),
                }
                for r, dist, flags in rows
            ],
        }

    @app.get("/lineage/cycles")
    def cycles():
        return {"cycles": [[{"kind": r.kind.value, "name": r.name} for r in c] for c in svc.lineage.find_cycles()]}

    # ---------------- 检索 ---------------- #
    @app.post("/search")
    def do_search(req: SearchRequest):
        criteria = SearchCriteria(
            name_contains=req.name_contains,
            tags=frozenset(req.tags),
            owner=req.owner,
            field_name_contains=req.field_name_contains,
        )
        return {"datasets": [_dataset_out(d) for d in svc.search(criteria)]}

    # ---------------- 审计 ---------------- #
    @app.get("/audit")
    def audit(target: str | None = None):
        return {
            "entries": [
                {
                    "sequence": e.sequence,
                    "timestamp": e.timestamp.isoformat(),
                    "actor": e.actor,
                    "action": e.action,
                    "target": e.target,
                    "changes": [
                        {"path": path, "old": _jsonable(old), "new": _jsonable(new)}
                        for path, old, new in e.changes
                    ],
                }
                for e in svc.history(target)
            ]
        }

    @app.get("/")
    def root():
        return {"service": "metadata-lineage-catalog", "status": "ok"}

    return app


def _jsonable(value: Any) -> Any:
    if isinstance(value, frozenset):
        return sorted(value)
    if isinstance(value, set):
        return sorted(value)
    return value
