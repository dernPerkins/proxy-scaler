"""MPC Fill art catalog endpoints: what's imported (local only), whether
a newer catalog is published, and the update job's lifecycle
(start/poll/cancel) plus delete. The catalog itself is mpc_catalog.py;
searching it happens behind the /api/mpcfill routes, through
art_sources.get_art_source."""

from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Response

from proxy_scaler import mpc_catalog, mpc_catalog_jobs
from proxy_scaler.api.deps import get_mpc_catalog_db_path
from proxy_scaler.api.schemas import (
    CardImportStartedOut,
    MpcCatalogCheckIn,
    MpcCatalogCheckOut,
    MpcCatalogImportStatusOut,
    MpcCatalogLocalOut,
    MpcCatalogStatusOut,
)

router = APIRouter(prefix="/api/mpccatalog", tags=["mpccatalog"])


@router.get("/status", response_model=MpcCatalogStatusOut)
def catalog_status() -> MpcCatalogStatusOut:
    """Answered from local state alone — the client reads this on launch,
    so it must never wait on the network (see cards.card_db_status)."""
    local = mpc_catalog.local_status(get_mpc_catalog_db_path())
    active = mpc_catalog_jobs.active_job()
    return MpcCatalogStatusOut(
        local=MpcCatalogLocalOut(**local) if local else None,
        import_running=active is not None,
        active_job_id=active.id if active else None,
    )


@router.post("/check", response_model=MpcCatalogCheckOut)
def check_catalog(body: MpcCatalogCheckIn) -> MpcCatalogCheckOut:
    """Is a newer catalog published? One manifest read, cached on the
    server for hours unless `refresh`. 503 when it can't be reached, so
    the client simply doesn't offer anything."""
    try:
        check = mpc_catalog.check_update(get_mpc_catalog_db_path(), refresh=body.refresh)
    except mpc_catalog.CatalogError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return MpcCatalogCheckOut(
        remote_version=check.remote_version,
        local_version=check.local_version,
        update_available=check.update_available,
        changed_sources=check.changed_sources,
        removed_sources=check.removed_sources,
        download_bytes=check.download_bytes,
        card_count=check.card_count,
    )


@router.post("/import", response_model=CardImportStartedOut, status_code=202)
def start_catalog_import() -> CardImportStartedOut:
    if mpc_catalog_jobs.active_job() is not None:
        raise HTTPException(
            status_code=409,
            detail="The MPC Fill catalog is already updating — wait for it to finish.",
        )
    job = mpc_catalog_jobs.create_job()
    threading.Thread(
        target=mpc_catalog.run_import,
        args=(job.id,),
        kwargs={"path": get_mpc_catalog_db_path()},
        daemon=True,
    ).start()
    return CardImportStartedOut(job_id=job.id)


@router.get("/import/{job_id}", response_model=MpcCatalogImportStatusOut)
def catalog_import_status(job_id: str) -> MpcCatalogImportStatusOut:
    job = mpc_catalog_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired update job.")
    return MpcCatalogImportStatusOut(
        status=job.status,
        phase=job.phase,
        bytes_downloaded=job.bytes_downloaded,
        total_bytes=job.total_bytes,
        rows_imported=job.rows_imported,
        error=job.error,
    )


@router.post("/import/{job_id}/cancel", status_code=204)
def cancel_catalog_import(job_id: str) -> Response:
    if not mpc_catalog_jobs.request_cancel(job_id):
        raise HTTPException(status_code=404, detail="Unknown or expired update job.")
    return Response(status_code=204)


@router.delete("", status_code=204)
def delete_catalog() -> Response:
    if mpc_catalog_jobs.active_job() is not None:
        raise HTTPException(
            status_code=409,
            detail="The MPC Fill catalog is updating — wait for it to finish.",
        )
    mpc_catalog.delete_catalog(get_mpc_catalog_db_path())
    return Response(status_code=204)
