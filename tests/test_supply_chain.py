from scripts.check_runtime_licenses import violations
from scripts.generate_sbom import build


def test_sbom_marks_runtime_extras_and_vendored_artifacts():
    components = build()["components"]
    scopes = {
        prop["value"]
        for component in components
        for prop in component.get("properties", [])
        if prop["name"] == "docket:dependency-scope"
    }
    assert {"runtime", "optional", "bundled-artifact"} <= scopes
    artefacts = [c for c in components if c.get("bom-ref", "").startswith("docket-artifact:")]
    assert artefacts and all(c["hashes"][0]["alg"] == "SHA-256" for c in artefacts)
    assert all(c["licenses"][0]["license"]["name"] for c in artefacts)


def test_runtime_license_check_rejects_copyleft_only():
    report = [
        {"Name": "ok", "Version": "1", "License": "Apache-2.0"},
        {"Name": "bad", "Version": "2", "License": "GNU LGPL v3"},
    ]
    assert violations(report) == ["bad 2: GNU LGPL v3"]
