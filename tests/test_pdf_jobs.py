"""Tests for pdf_jobs.py: the in-memory PDF render job registry."""

from __future__ import annotations

import pytest

from proxy_scaler import pdf_jobs


@pytest.fixture(autouse=True)
def _clean_registry():
    pdf_jobs._reset_for_tests()
    yield
    pdf_jobs._reset_for_tests()


def test_create_and_progress() -> None:
    job = pdf_jobs.create_job(filename="deck.pdf", total=3)
    assert job.status == pdf_jobs.RENDERING
    assert job.completed == 0

    pdf_jobs.set_progress(job.id, 2)
    assert pdf_jobs.get(job.id).completed == 2


def test_finish_makes_result_available_exactly_once() -> None:
    """pop_result evicts on read — the client fetches a finished PDF once,
    and holding multi-MB buffers past that is the whole thing this
    registry has to avoid."""
    job = pdf_jobs.create_job(filename="deck.pdf", total=1)
    pdf_jobs.finish(job.id, b"%PDF-fake")

    stored = pdf_jobs.get(job.id)
    assert stored.status == pdf_jobs.DONE
    assert stored.completed == stored.total  # finishing implies fully done

    assert pdf_jobs.pop_result(job.id) == ("deck.pdf", b"%PDF-fake")
    assert pdf_jobs.pop_result(job.id) is None
    assert pdf_jobs.get(job.id) is None


def test_pop_result_refuses_unfinished_job_without_evicting_it() -> None:
    job = pdf_jobs.create_job(filename="deck.pdf", total=2)
    assert pdf_jobs.pop_result(job.id) is None
    assert pdf_jobs.get(job.id) is not None  # still rendering, still tracked


def test_failed_job_keeps_its_error() -> None:
    job = pdf_jobs.create_job(filename="deck.pdf", total=1)
    pdf_jobs.fail(job.id, "boom")

    stored = pdf_jobs.get(job.id)
    assert stored.status == pdf_jobs.FAILED
    assert stored.error == "boom"
    assert pdf_jobs.pop_result(job.id) is None


def test_cancel_flag_is_visible_to_the_render_thread() -> None:
    job = pdf_jobs.create_job(filename="deck.pdf", total=5)
    assert pdf_jobs.is_cancel_requested(job.id) is False

    assert pdf_jobs.request_cancel(job.id) is True
    assert pdf_jobs.is_cancel_requested(job.id) is True
    # Still RENDERING: the flag is a request, the render thread is what
    # actually moves it to CANCELED once it notices.
    assert pdf_jobs.get(job.id).status == pdf_jobs.RENDERING

    pdf_jobs.mark_canceled(job.id)
    assert pdf_jobs.get(job.id).status == pdf_jobs.CANCELED
    assert pdf_jobs.get(job.id).data is None


def test_request_cancel_on_unknown_job_is_false_not_an_error() -> None:
    assert pdf_jobs.request_cancel("nope") is False
    assert pdf_jobs.is_cancel_requested("nope") is False


def test_updates_to_evicted_job_are_silent_noops() -> None:
    """The render thread can outlive its job (cancel + evict, or a TTL
    sweep); its remaining progress/finish calls must not explode."""
    job = pdf_jobs.create_job(filename="deck.pdf", total=1)
    pdf_jobs.finish(job.id, b"x")
    pdf_jobs.pop_result(job.id)  # evicted

    pdf_jobs.set_progress(job.id, 1)
    pdf_jobs.finish(job.id, b"y")
    pdf_jobs.fail(job.id, "late")
    pdf_jobs.mark_canceled(job.id)
    assert pdf_jobs.get(job.id) is None


def test_create_job_sweeps_expired_entries(monkeypatch) -> None:
    """An abandoned job (client never fetched the result) must not pin its
    buffer for the life of the process."""
    stale = pdf_jobs.create_job(filename="old.pdf", total=1)
    pdf_jobs.finish(stale.id, b"%PDF-old")

    # Jump past the TTL rather than sleeping through it.
    real_monotonic = pdf_jobs.time.monotonic
    monkeypatch.setattr(
        pdf_jobs.time, "monotonic", lambda: real_monotonic() + pdf_jobs._TTL_SECONDS + 1
    )
    fresh = pdf_jobs.create_job(filename="new.pdf", total=1)

    assert pdf_jobs.get(stale.id) is None
    assert pdf_jobs.get(fresh.id) is not None


def test_active_count_tracks_only_rendering_jobs() -> None:
    a = pdf_jobs.create_job(filename="a.pdf", total=1)
    b = pdf_jobs.create_job(filename="b.pdf", total=1)
    assert pdf_jobs.active_count() == 2

    pdf_jobs.finish(a.id, b"x")
    assert pdf_jobs.active_count() == 1

    pdf_jobs.fail(b.id, "nope")
    assert pdf_jobs.active_count() == 0
