"""Durable HTTP jobs: one directory per job, idempotent creation, results in
document order under the caller's filenames, uploads removed on request."""
import pytest

from docket import job_store
from docket.job_store import JobOptions
from tests.factories import make_result


@pytest.fixture(autouse=True)
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(job_store.config, "JOBS_DIR", tmp_path / "jobs")
    return tmp_path / "jobs"


def _staged(tmp_path, name, content="invoice"):
    path = job_store.incoming_dir() / name
    path.write_text(content)
    return path


def test_jobs_are_durable_and_idempotent(tmp_path, jobs_dir):
    first, created = job_store.create(
        [("a.txt", _staged(tmp_path, "x1.txt"))], JobOptions(), idempotency_key="request-1"
    )
    second, created_again = job_store.create(
        [("a.txt", _staged(tmp_path, "x2.txt"))], JobOptions(), idempotency_key="request-1"
    )
    assert created and not created_again
    assert first.job_id == second.job_id
    assert not (jobs_dir / "_incoming" / "x2.txt").exists()  # the duplicate upload is discarded
    assert [j.job_id for j in job_store.unfinished()] == [first.job_id]
    done = job_store.update(first.job_id, status="completed")
    assert done.status == "completed" and job_store.unfinished() == []


def test_uploads_are_stored_by_index_not_by_caller_name(tmp_path):
    job, _ = job_store.create(
        [("../../etc/passwd.txt", _staged(tmp_path, "a.txt")), ("b.pdf", _staged(tmp_path, "b.pdf"))],
        JobOptions(),
    )
    assert [d.stored_as for d in job.documents] == ["00000.txt", "00001.pdf"]
    assert all(p.parent == job_store.uploads_dir(job.job_id) for p in job_store.document_paths(job))


def test_results_come_back_in_document_order_with_caller_names(tmp_path):
    job, _ = job_store.create(
        [("first.txt", _staged(tmp_path, "1.txt")), ("second.txt", _staged(tmp_path, "2.txt"))], JobOptions()
    )
    paths = job_store.document_paths(job)
    with job_store.results_path(job.job_id).open("w") as handle:
        for path in reversed(paths):  # finished out of order
            handle.write(make_result(source=str(path)).model_dump_json() + "\n")
    names = [(d.index, r.source) for d, r in job_store.results(job)]
    assert names == [(0, "first.txt"), (1, "second.txt")]


def test_unknown_or_malicious_ids_are_not_found():
    assert job_store.get("job_doesnotexist") is None
    assert job_store.get("../../etc") is None


def test_uploads_can_be_removed(tmp_path):
    job, _ = job_store.create([("a.txt", _staged(tmp_path, "a.txt"))], JobOptions())
    job_store.remove_uploads(job.job_id)
    assert not job_store.uploads_dir(job.job_id).exists()
    assert job_store.get(job.job_id) is not None
