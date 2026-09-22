"""Official e-invoice validation and the EN 16931 exporters.

Positive cases are the official examples shipped with each artifact release;
negative cases mutate one thing in an otherwise valid invoice and check the
rule id the official Schematron reports for it.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pypdfium2 as pdfium
import pytest
from lxml import etree

from docket import cli
from docket.catalog import CreditNote, Invoice
from docket.einvoice import (
    EInvoiceResourcesMissing,
    EInvoiceUnavailable,
    EInvoiceValidationOptions,
    PdfAValidationResult,
    Profile,
    artifacts,
    extract_facturx_xml,
    fetch,
    generate_facturx_pdf,
    validate_pdfa,
    validate_einvoice,
    verify_facturx_round_trip,
    validator,
)
from docket.export import ExportError, ExportOptions, export_document

FIXTURES = Path(__file__).parent / "fixtures" / "einvoice"
OFFICIAL = FIXTURES / "official"
NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
}
EINVOICE_FORMATS = ["ubl", "peppol", "xrechnung-ubl", "xrechnung-cii", "factur-x-en16931", "factur-x-basic"]


def _data() -> dict:
    return json.loads((FIXTURES / "complete_invoice.json").read_text(encoding="utf-8"))


def complete_invoice(**overrides) -> Invoice:
    data = _data()
    data.update(overrides)
    return Invoice.model_validate(data)


def peppol_invoice() -> Invoice:
    """Peppol only routes to EAS-coded endpoints, not e-mail addresses."""
    data = _data()
    data["seller"].update(electronic_address="DE136695976", electronic_address_scheme="9930")
    data["buyer"].update(electronic_address="4000001987658", electronic_address_scheme="0088")
    return Invoice.model_validate(data)


def invoice_for(fmt: str) -> Invoice:
    return peppol_invoice() if fmt == "peppol" else complete_invoice()


def xml_for(fmt: str) -> bytes:
    return export_document(invoice_for(fmt), fmt).content.encode()


def credit_note() -> CreditNote:
    data = _data()
    data["credit_note_number"] = "GS-2026-0007"
    data.pop("invoice_number")
    data["references"] = [{"kind": "invoice", "number": "RE-2026-0418", "issue_date": "2026-05-04"}]
    data["reason"] = "Returned goods"
    return CreditNote.model_validate(data)


def errors(result) -> set[str]:
    return {i.code for i in result.issues if i.severity in ("fatal", "error")}


def mutated(fmt: str, change, document=None) -> bytes:
    tree = etree.fromstring(export_document(document or invoice_for(fmt), fmt).content.encode())
    change(tree)
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8")


def remove(path: str):
    def change(tree):
        node = tree.find(path, NS)
        node.getparent().remove(node)
    return change


def set_text(path: str, value: str):
    def change(tree):
        tree.find(path, NS).text = value
    return change


def set_attribute(path: str, name: str, value: str):
    def change(tree):
        tree.find(path, NS).set(name, value)
    return change


# ---- official positive examples -----------------------------------------------------------


def _official(folder: str, *patterns: str) -> list[Path]:
    return sorted(p for pattern in patterns for p in (OFFICIAL / folder).glob(pattern))


@pytest.mark.parametrize(
    "path",
    _official("en16931-ubl", "ubl-tc434-*.xml", "guide-example*.xml", "BIS3_Invoice_*.XML"),
    ids=lambda p: p.name,
)
def test_official_en16931_ubl_examples_are_valid(path):
    result = validate_einvoice(path)
    assert result.valid, sorted(errors(result))
    assert result.syntax == "ubl"
    assert result.profile in (Profile.EN16931, Profile.PEPPOL)
    assert result.layers[0].layer == "xsd" and len(result.layers) >= 2


@pytest.mark.parametrize("path", _official("en16931-cii", "CII_example*.xml", "CII_business_example_*.xml"),
                         ids=lambda p: p.name)
def test_official_en16931_cii_examples_are_valid(path):
    # CEN's CII examples declare assorted guideline ids; validate them against
    # the plain EN 16931 rules (the Factur-X profiles add their own).
    result = validate_einvoice(path, EInvoiceValidationOptions(profile=Profile.EN16931))
    assert not errors(result) - {"DOCKET-PROFILE-MISMATCH"}, sorted(errors(result))
    assert all(layer.passed for layer in result.layers)


@pytest.mark.parametrize("path", _official("xrechnung", "*.xml"), ids=lambda p: p.name)
def test_official_xrechnung_testsuite_instances_are_valid(path):
    result = validate_einvoice(path)
    assert result.valid, sorted(errors(result))
    assert result.profile in (Profile.XRECHNUNG, Profile.FACTURX_XRECHNUNG)
    # XSD, EN 16931 rules, XRechnung CIUS rules.
    assert len(result.layers) == 3


@pytest.mark.parametrize("path", _official("peppol/examples", "*.xml"), ids=lambda p: p.name)
def test_official_peppol_examples_are_valid(path):
    result = validate_einvoice(path)
    assert result.valid, sorted(errors(result))
    assert result.profile is Profile.PEPPOL


# ---- Peppol's own conformance suite ----------------------------------------------------------


def _peppol_unit_cases():
    vefa = "{http://difi.no/xsd/vefa/validator/1.0}"
    for path in sorted((OFFICIAL / "peppol" / "unit-UBL-PEPPOL").glob("*.xml")):
        tree = etree.parse(str(path))
        for number, test in enumerate(tree.getroot().iter(f"{vefa}test"), 1):
            expectation = test.find(f"{vefa}assert")
            document = next(child for child in test if not child.tag.startswith(vefa))
            yield pytest.param(
                etree.tostring(document),
                {e.text.strip() for e in expectation.iter(f"{vefa}success")},
                {e.text.strip() for e in expectation.iter(f"{vefa}error")},
                {e.text.strip() for e in expectation.iter(f"{vefa}warning")},
                id=f"{path.stem}-{number}",
            )


PEPPOL_UNIT = list(_peppol_unit_cases())


def test_peppol_unit_suite_is_complete():
    assert len(PEPPOL_UNIT) == 227, "run scripts/update_einvoice_resources.py --fixtures-only"


@pytest.mark.parametrize("xml, success, failing, warning", PEPPOL_UNIT)
def test_peppol_unit_conformance(xml, success, failing, warning):
    """The fragments are partial invoices (not XSD-valid), so the Peppol
    Schematron runs on them directly, as in Peppol's own test harness."""
    engine = validator._get_engine()
    issues, _ = validator._parse_svrl(engine.svrl("peppol/PEPPOL-EN16931-UBL.xsl", xml), "peppol")
    fired = {i.code for i in issues if i.severity in ("fatal", "error")}
    warned = {i.code for i in issues if i.severity == "warning"}
    assert failing <= fired
    assert warning <= warned
    assert not success & (fired | warned)


# ---- negative cases ----------------------------------------------------------------------------


def test_missing_mandatory_field():
    xml = mutated("ubl", remove("cac:AccountingCustomerParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName"))
    result = validate_einvoice(xml)
    assert not result.valid
    assert "BR-07" in errors(result)  # buyer name


def test_missing_xrechnung_buyer_reference():
    result = validate_einvoice(mutated("xrechnung-ubl", remove("cbc:BuyerReference")))
    assert "BR-DE-15" in errors(result)
    issue = next(i for i in result.issues if i.code == "BR-DE-15")
    assert issue.layer == "schematron" and "xrechnung" in issue.rule_source.lower()


def test_wrong_totals_break_a_business_rule_on_xsd_valid_xml():
    result = validate_einvoice(mutated("ubl", set_text("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", "1500.00")))
    assert not result.valid
    xsd, schematron = result.layers
    assert xsd.passed and not schematron.passed
    assert {"BR-CO-15", "BR-CO-16"} <= errors(result)
    issue = next(i for i in result.issues if i.code == "BR-CO-15")
    assert issue.location and issue.location.startswith("/")
    assert "EN 16931" in issue.rule_source or "1.3.16" in issue.rule_source


def test_wrong_totals_in_cii():
    path = ".//ram:SpecifiedTradeSettlementHeaderMonetarySummation/ram:GrandTotalAmount"
    result = validate_einvoice(mutated("factur-x-en16931", set_text(path, "1.00")))
    assert not result.valid
    assert result.layers[0].passed and not result.layers[1].passed


def test_wrong_tax_category():
    # Lines say "zero rated" while charging 19 %.
    result = validate_einvoice(mutated("ubl", set_text("cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:ID", "Z")))
    assert {"BR-Z-05", "BR-S-08"} <= errors(result)


def test_unknown_tax_category_code():
    result = validate_einvoice(mutated("ubl", set_text("cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:ID", "Q")))
    assert "BR-CL-18" in errors(result)


def test_bad_endpoint_scheme():
    xml = mutated("peppol", set_attribute("cac:AccountingSupplierParty/cac:Party/cbc:EndpointID", "schemeID", "ZZZZ"))
    result = validate_einvoice(xml)
    assert "PEPPOL-EN16931-CL008" in errors(result)


def test_unknown_profile_identifier():
    result = validate_einvoice(mutated("ubl", set_text("cbc:CustomizationID", "urn:example:not-a-profile")))
    assert not result.valid
    assert errors(result) == {"DOCKET-PROFILE-UNKNOWN"}
    assert result.declared_profile_id == "urn:example:not-a-profile"


def test_declared_profile_must_match_the_requested_one():
    xml = export_document(peppol_invoice(), "peppol").content.encode()
    result = validate_einvoice(xml, EInvoiceValidationOptions(profile=Profile.XRECHNUNG))
    assert not result.valid
    assert "DOCKET-PROFILE-MISMATCH" in errors(result)
    # The rules still ran, so the caller sees everything else too.
    assert all(layer.ran for layer in result.layers)


def test_xsd_failure_skips_schematron():
    result = validate_einvoice(mutated("ubl", remove("cbc:IssueDate")))
    xsd, schematron = result.layers
    assert not xsd.passed and xsd.ran
    assert not schematron.ran and schematron.skipped_reason
    assert "XSD" in errors(result)


@pytest.mark.parametrize(
    "data, code",
    [(b"not xml", "DOCKET-XML"), (b"<root/>", "DOCKET-FORMAT"), (b"%PDF-1.7\n", "DOCKET-INPUT")],
)
def test_unusable_input_is_a_fatal_issue(data, code):
    result = validate_einvoice(data)
    assert not result.valid and errors(result) == {code}


def test_documents_with_a_dtd_are_refused(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    xml = export_document(complete_invoice(), "ubl").content
    xml = xml.replace("<?xml version='1.0' encoding='UTF-8'?>", "").replace('<?xml version="1.0" encoding="UTF-8"?>', "")
    xml = f'<!DOCTYPE Invoice [<!ENTITY x SYSTEM "file://{secret}">]>' + xml.replace("RE-2026-0418", "&x;", 1)
    result = validate_einvoice(xml.encode())
    assert errors(result) == {"DOCKET-XML"}
    assert "TOP-SECRET" not in result.model_dump_json()


# ---- containers, metadata, artifacts ---------------------------------------------------------------


def _facturx_pdf(tmp_path: Path, xml: bytes, name: str = "factur-x.xml") -> Path:
    document = pdfium.PdfDocument.new()
    document.new_page(595, 842)
    attachment = document.new_attachment(name)
    attachment.set_data(xml)
    path = tmp_path / "invoice.pdf"
    document.save(str(path))
    document.close()
    return path


def _blank_pdf() -> bytes:
    from io import BytesIO
    from pypdf import PdfWriter

    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(output)
    return output.getvalue()


def test_factur_x_pdf_generation_embeds_xml_and_xmp():
    from io import BytesIO
    from pypdf import PdfReader

    xml = export_document(complete_invoice(), "factur-x-en16931").content.encode()
    pdf = generate_facturx_pdf(_blank_pdf(), xml, level="en16931")
    reader = PdfReader(BytesIO(pdf))
    xmp = reader.root_object["/Metadata"].get_data()

    assert extract_facturx_xml(pdf) == xml
    assert reader.attachments["factur-x.xml"][0] == xml
    assert b"urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#" in xmp


def test_verapdf_report_is_parsed(monkeypatch, tmp_path):
    report = b'''<?xml version="1.0"?><report><buildInformation>
      <releaseDetails id="core" version="1.28.2"/></buildInformation><jobs><job>
      <validationReport profileName="PDF/A-3b validation profile" isCompliant="false">
      <details passedRules="99" failedRules="1" passedChecks="400" failedChecks="1">
      <rule specification="ISO 19005-3:2012" clause="6.2.2" testNumber="1" status="failed">
      <description>Output intent missing</description><check status="failed"><context>root</context></check>
      </rule></details></validationReport></job></jobs></report>'''
    executable = tmp_path / "verapdf"
    executable.write_text("placeholder")
    monkeypatch.setattr("docket.einvoice.facturx_pdf.subprocess.run", lambda *a, **k: type(
        "Completed", (), {"stdout": report, "stderr": b"", "returncode": 0}
    )())

    result = validate_pdfa(_blank_pdf(), executable=str(executable))

    assert not result.compliant
    assert result.validator_version == "1.28.2"
    assert result.failed_rules == 1
    assert result.issues[0].clause == "6.2.2"
    assert result.issues[0].context == "root"


def test_factur_x_round_trip_checks_both_layers(monkeypatch):
    xml = export_document(complete_invoice(), "factur-x-en16931").content.encode()
    pdf = generate_facturx_pdf(_blank_pdf(), xml, level="en16931")
    monkeypatch.setattr(
        "docket.einvoice.facturx_pdf.validate_pdfa",
        lambda *a, **k: PdfAValidationResult(compliant=True, profile="PDF/A-3b validation profile"),
    )

    result = verify_facturx_round_trip(pdf, expected_xml=xml)

    assert result.valid
    assert result.xml_matches
    assert result.xml_validation.valid


def test_factur_x_pdf_is_validated_from_its_attachment(tmp_path):
    xml = export_document(complete_invoice(), "factur-x-en16931").content.encode()
    result = validate_einvoice(_facturx_pdf(tmp_path, xml))
    assert result.valid, sorted(errors(result))
    assert result.detected_format == "factur-x-pdf/cii"
    assert result.profile is Profile.FACTURX_EN16931


def test_pdf_without_an_e_invoice_attachment(tmp_path):
    result = validate_einvoice(_facturx_pdf(tmp_path, b"<x/>", name="notes.xml"))
    assert errors(result) == {"DOCKET-INPUT"}
    assert "notes.xml" in result.issues[0].message


def test_result_reports_validator_and_resource_versions():
    result = validate_einvoice(export_document(complete_invoice(), "xrechnung-ubl").content.encode())
    assert result.valid
    assert result.detected_format == "ubl-invoice"
    assert result.profile is Profile.XRECHNUNG
    assert result.declared_profile_id.endswith("xrechnung_3.0")
    assert "saxon" in result.validator_version.lower() and "lxml" in result.validator_version
    assert "1.3.16" in result.validation_resource_version
    assert "2.6.0" in result.validation_resource_version
    for layer in result.layers:
        assert layer.version and layer.artifact
    assert result.layers[1].rules_fired and result.layers[1].rules_fired > 0


def test_vendored_artifacts_match_their_manifest():
    assert artifacts.verify() == []
    manifest = artifacts.manifest()
    ids = {entry["id"] for entry in manifest["artifacts"]}
    assert {"kosit-configuration", "uncefact-cii-d16b", "en16931-ubl", "en16931-cii", "xrechnung-schematron",
            "peppol-bis-billing", "factur-x"} <= ids
    for entry in manifest["artifacts"]:
        assert entry["version"] and entry["license"] and entry["url"].startswith("https://")


def test_vendored_artifact_license_files_are_shipped():
    manifest = artifacts.manifest()
    for entry in manifest["artifacts"]:
        assert isinstance(entry["license"], str) and entry["license"].strip()
        for relative in entry.get("license_files", []):
            license_path = artifacts.root() / relative
            assert license_path.is_file(), f"{entry['id']}: missing {relative}"
            assert license_path.read_text(encoding="utf-8").strip()
        assert len(entry["sha256"]) == 64
        # The XRechnung test suite only supplies test fixtures.
        assert entry["files"] or entry["id"] == "xrechnung-testsuite"


def test_tampered_artifact_is_detected(tmp_path, monkeypatch):
    import shutil

    copy_root = tmp_path / "resources"
    shutil.copytree(artifacts.root(), copy_root)
    target = copy_root / "en16931" / "EN16931-UBL-validation.xslt"
    target.write_text(target.read_text() + "<!-- edited -->")
    monkeypatch.setattr(artifacts.config, "EINVOICE_RESOURCES", str(copy_root))
    assert artifacts.verify() == ["checksum mismatch: en16931/EN16931-UBL-validation.xslt (en16931-ubl)"]


# ---- artifacts Docket does not ship ------------------------------------------------------------

RESTRICTED = ("uncefact-cii-d16b", "peppol-bis-billing", "factur-x")


def test_only_artifacts_with_verified_terms_are_shipped():
    shipped = {e["id"] for e in artifacts.manifest()["artifacts"] if artifacts.redistributable(e)}
    assert not shipped & set(RESTRICTED)
    assert set(fetch.downloadable()) == set(RESTRICTED)


@pytest.fixture
def shipped_only(tmp_path, monkeypatch):
    """A resource root with only what the wheel ships, and an empty download root."""
    import shutil

    restricted_dirs = {relative.split("/")[0] for a in RESTRICTED for relative in artifacts.artifact(a)["files"]}
    # Where this checkout has them (resources or downloads), for _install_from_checkout.
    originals = {r: artifacts.path(r) for a in RESTRICTED for r in artifacts.artifact(a)["files"]}
    root = tmp_path / "resources"
    shutil.copytree(artifacts.root(), root, ignore=lambda d, names: [n for n in names if n in restricted_dirs])
    downloads = tmp_path / "downloads"
    monkeypatch.setattr(artifacts.config, "EINVOICE_RESOURCES", str(root))
    monkeypatch.setattr(artifacts.config, "EINVOICE_DOWNLOADS", str(downloads))
    return SimpleNamespace(downloads=downloads, originals=originals)


def test_profiles_without_downloads_name_the_fetch_command(shipped_only):
    assert validate_einvoice(xml_for("ubl")).valid  # shipped: works as installed
    assert artifacts.verify() == []
    for fmt in ("peppol", "xrechnung-cii", "factur-x-en16931"):
        with pytest.raises(EInvoiceResourcesMissing, match="docket einvoice fetch"):
            validate_einvoice(xml_for(fmt))
    assert issubclass(EInvoiceResourcesMissing, EInvoiceUnavailable)


def _install_from_checkout(monkeypatch, originals: dict, corrupt: str | None = None):
    """Stand in for the network: install an artifact's files from this checkout."""

    def install(source, dest_root, cache=None, fixtures_root=None):
        written = []
        for relative in artifacts.artifact(source["id"])["files"]:
            target = dest_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            data = originals[relative].read_bytes()
            target.write_bytes(data + b"<!-- edited -->" if relative == corrupt else data)
            written.append(target)
        return written

    monkeypatch.setattr(fetch.sources, "install", install)


def test_fetch_installs_verified_artifacts(shipped_only, monkeypatch):
    _install_from_checkout(monkeypatch, shipped_only.originals)
    assert fetch.fetch_einvoice_resources() == list(RESTRICTED)
    assert artifacts.missing(RESTRICTED) == []
    assert artifacts.verify() == []
    assert validate_einvoice(xml_for("factur-x-en16931")).valid
    assert fetch.fetch_einvoice_resources() == []  # present and intact


def test_fetch_refuses_files_that_do_not_match_the_manifest(shipped_only, monkeypatch):
    _install_from_checkout(monkeypatch, shipped_only.originals, corrupt="factur-x/basic/Factur-X_BASIC.xsd")
    with pytest.raises(artifacts.ArtifactError, match="does not match the manifest"):
        fetch.fetch_einvoice_resources(["factur-x"])
    assert not (shipped_only.downloads / "factur-x").exists()


def test_cli_einvoice_status_and_fetch(shipped_only, monkeypatch, capsys):
    _install_from_checkout(monkeypatch, shipped_only.originals)
    cli.main(["einvoice", "status"])
    assert "peppol-bis-billing" in capsys.readouterr().out
    cli.main(["einvoice", "fetch", "peppol-bis-billing"])
    out = capsys.readouterr().out
    assert "upstream terms apply" in out and "downloaded: peppol-bis-billing" in out
    cli.main(["einvoice", "status"])
    assert "downloaded" in capsys.readouterr().out


def test_missing_extra_is_a_configuration_error(monkeypatch):
    monkeypatch.setattr(validator, "available", lambda: False)
    with pytest.raises(EInvoiceUnavailable, match=r"docket-idp\[einvoice\]"):
        validate_einvoice(b"<x/>")


def test_base_install_works_without_the_extra():
    import subprocess
    import sys

    code = (
        "import sys; sys.modules['lxml'] = None; sys.modules['saxonche'] = None\n"
        "import docket\n"
        "from docket import EInvoiceUnavailable, export_document, validate_einvoice\n"
        "assert not docket.einvoice.available()\n"
        "try:\n    validate_einvoice(b'<x/>')\nexcept EInvoiceUnavailable as exc:\n    print('unavailable', exc)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.startswith("unavailable") and "docket-idp[einvoice]" in out


# ---- exporters ----------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", EINVOICE_FORMATS)
def test_every_einvoice_export_passes_its_official_rules(fmt):
    exported = export_document(invoice_for(fmt), fmt, ExportOptions(validate_einvoice=True))
    report = exported.einvoice_validation
    assert report is not None
    assert report.valid, sorted(errors(report))


@pytest.mark.parametrize("fmt", EINVOICE_FORMATS)
def test_credit_notes_export_as_credit_notes(fmt):
    document = credit_note()
    if fmt == "peppol":
        document = document.model_copy(update={"seller": peppol_invoice().seller, "buyer": peppol_invoice().buyer})
    exported = export_document(document, fmt, ExportOptions(validate_einvoice=True))
    assert exported.einvoice_validation.valid, sorted(errors(exported.einvoice_validation))
    tree = etree.fromstring(exported.content.encode())
    if exported.content.lstrip().startswith("<?xml") and "CrossIndustryInvoice" in tree.tag:
        assert tree.findtext("rsm:ExchangedDocument/ram:TypeCode", namespaces=NS) == "381"
    else:
        assert tree.tag.endswith("}CreditNote")
        assert tree.findtext("cbc:CreditNoteTypeCode", namespaces=NS) == "381"
    assert "RE-2026-0418" in exported.content  # preceding invoice reference (BT-25)


def test_exported_amounts_and_parties_round_trip():
    tree = etree.fromstring(export_document(complete_invoice(), "ubl").content.encode())
    assert tree.findtext("cbc:ID", namespaces=NS) == "RE-2026-0418"
    assert tree.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=NS) == "1499.40"
    assert tree.findtext("cac:TaxTotal/cbc:TaxAmount", namespaces=NS) == "239.40"
    assert tree.findtext(
        "cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID", namespaces=NS
    ) == "DE136695976"
    assert len(tree.findall("cac:InvoiceLine", NS)) == 2


@pytest.mark.parametrize(
    "change, message",
    [
        ({"line_items": []}, "BR-16"),
        ({"tax_amount": 250.0, "total_amount": 1510.0}, "tax"),
        ({"subtotal": 1300.0, "tax_amount": 247.0, "total_amount": 1547.0}, "1260.00"),
    ],
)
def test_exporter_refuses_what_it_cannot_represent(change, message):
    with pytest.raises(ExportError, match=message):
        export_document(complete_invoice(**change), "ubl")


def test_validating_a_non_einvoice_format_is_an_error():
    with pytest.raises(ExportError, match="not an e-invoice format"):
        export_document(complete_invoice(), "xero-json", ExportOptions(validate_einvoice=True))


# ---- CLI and HTTP ---------------------------------------------------------------------------------


@pytest.fixture
def xml_files(tmp_path):
    good = tmp_path / "good.xml"
    good.write_text(export_document(complete_invoice(), "xrechnung-ubl").content, encoding="utf-8")
    bad = tmp_path / "bad.xml"
    bad.write_bytes(mutated("xrechnung-ubl", remove("cbc:BuyerReference")))
    return good, bad


def run_cli(*argv: str) -> int:
    try:
        cli.main(list(argv))
    except SystemExit as exc:
        return exc.code
    return 0


def test_cli_validate_einvoice_exit_codes(xml_files, capsys):
    good, bad = xml_files
    assert run_cli("validate-einvoice", str(good)) == 0
    assert "VALID" in capsys.readouterr().out
    assert run_cli("validate-einvoice", str(bad), "--format", "json") == 2
    report = json.loads(capsys.readouterr().out)
    assert report["valid"] is False
    assert any(i["code"] == "BR-DE-15" for i in report["issues"])


def test_cli_validate_einvoice_profile_mismatch(xml_files, capsys):
    good, _ = xml_files
    assert run_cli("validate-einvoice", str(good), "--profile", "peppol") == 2
    assert "DOCKET-PROFILE-MISMATCH" in capsys.readouterr().out


def test_cli_validate_einvoice_without_the_extra(xml_files, monkeypatch, capsys):
    monkeypatch.setattr(validator, "available", lambda: False)
    assert run_cli("validate-einvoice", str(xml_files[0])) == 3
    assert "docket-idp[einvoice]" in capsys.readouterr().err


def test_cli_process_can_validate_its_export(monkeypatch, capsys):
    from tests.factories import make_result

    result = make_result(source="x.pdf", extracted=complete_invoice().model_dump(mode="json"))
    monkeypatch.setattr(cli, "process_document", lambda _path, _options: result)
    assert run_cli("process", "x.pdf", "--export", "xrechnung-ubl", "--validate-export") == 0
    captured = capsys.readouterr()
    assert "RE-2026-0418" in captured.out
    assert "valid" in captured.err.lower()


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from docket import api

    monkeypatch.setattr(api.config, "API_KEY", None)
    with TestClient(api.app) as test_client:
        yield test_client


def test_http_validate_einvoice(client, xml_files):
    good, bad = xml_files
    ok = client.post("/validate/einvoice", files={"file": ("good.xml", good.read_bytes(), "application/xml")})
    assert ok.status_code == 200 and ok.json()["valid"] is True
    failed = client.post(
        "/validate/einvoice", files={"file": ("bad.xml", bad.read_bytes(), "application/xml")}
    )
    assert failed.status_code == 200 and failed.json()["valid"] is False
    as_peppol = client.post(
        "/validate/einvoice", files={"file": ("good.xml", good.read_bytes())}, data={"profile": "peppol"}
    )
    assert any(i["code"] == "DOCKET-PROFILE-MISMATCH" for i in as_peppol.json()["issues"])


def test_http_validate_einvoice_errors(client, monkeypatch, xml_files):
    from docket import api

    wrong_type = client.post("/validate/einvoice", files={"file": ("x.txt", b"hi")})
    assert wrong_type.status_code == 415 and wrong_type.json()["error"]["code"] == "unsupported_file_type"
    bad_profile = client.post("/validate/einvoice", files={"file": ("x.xml", b"<x/>")}, data={"profile": "nope"})
    assert bad_profile.status_code == 422
    monkeypatch.setattr(api.config, "MAX_FILE_BYTES", 10)
    too_large = client.post("/validate/einvoice", files={"file": ("x.xml", xml_files[0].read_bytes())})
    assert too_large.status_code == 413
    monkeypatch.setattr(api.config, "MAX_FILE_BYTES", 10_000_000)
    monkeypatch.setattr(validator, "available", lambda: False)
    unavailable = client.post("/validate/einvoice", files={"file": ("x.xml", xml_files[0].read_bytes())})
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "einvoice_unavailable"


# ---- amount tolerance (absolute, one cent) ---------------------------------------------------------


def test_extraction_tolerance_is_one_cent():
    from docket.validate import validate

    data = _data()
    ok = validate(Invoice.model_validate({**data, "total_amount": 1499.41}))
    assert not any(i.severity == "error" and "total" in i.field for i in ok)
    off = validate(Invoice.model_validate({**data, "total_amount": 1499.42}))
    assert any(i.severity == "error" and i.field == "total_amount" for i in off)


def test_relative_tolerance_no_longer_hides_large_differences():
    from docket.validate import validate

    data = copy.deepcopy(_data())
    # 1 % of 1100 would have let a 10.00 gap through.
    data.update(subtotal=1260.0, tax_amount=239.4, total_amount=1509.4)
    issues = validate(Invoice.model_validate(data))
    assert any(i.severity == "error" for i in issues)
