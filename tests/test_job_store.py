from docket import job_store


def test_jobs_are_durable_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(job_store.config, "JOB_STORE_PATH", tmp_path / "jobs.json")
    source = tmp_path / "upload.txt"
    source.write_text("invoice")

    first = job_store.create(source, "invoice.txt", idempotency_key="request-1")
    second = job_store.create(source, "invoice.txt", idempotency_key="request-1")
    completed = job_store.update(first["job_id"], status="completed", result={"ok": True})

    assert first["job_id"] == second["job_id"]
    assert job_store.get(first["job_id"])["result"] == {"ok": True}
    assert completed["status"] == "completed"
    assert job_store.unfinished() == []
