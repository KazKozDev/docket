"""Create and verify Factur-X hybrid PDF/A-3 documents."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from pydantic import BaseModel, Field

from ..errors import ConfigurationError
from .validator import EInvoiceValidationOptions, EInvoiceValidationResult, Profile, validate_einvoice

Source = str | Path | bytes
FacturXLevel = Literal["minimum", "basicwl", "basic", "en16931", "extended", "autodetect"]


class FacturXPdfUnavailable(ConfigurationError):
    """A required Factur-X PDF/A dependency is unavailable."""


class PdfAValidationIssue(BaseModel):
    specification: str | None = None
    clause: str | None = None
    test_number: str | None = None
    description: str
    context: str | None = None


class PdfAValidationResult(BaseModel):
    compliant: bool
    profile: str
    validator: str = "veraPDF"
    validator_version: str | None = None
    passed_rules: int = 0
    failed_rules: int = 0
    passed_checks: int = 0
    failed_checks: int = 0
    issues: list[PdfAValidationIssue] = Field(default_factory=list)


class FacturXRoundTripResult(BaseModel):
    valid: bool
    xml_matches: bool
    xml_validation: EInvoiceValidationResult
    pdfa_validation: PdfAValidationResult


def _bytes(source: Source) -> bytes:
    return source if isinstance(source, bytes) else Path(source).read_bytes()


def _require_facturx() -> None:
    if importlib.util.find_spec("facturx") is None:
        raise FacturXPdfUnavailable(
            'Factur-X PDF generation needs `pip install "docket-idp[einvoice]"`'
        )


def generate_facturx_pdf(
    source_pdf: Source,
    xml: Source,
    *,
    level: FacturXLevel = "autodetect",
    lang: str | None = None,
    validate_xml: bool = True,
) -> bytes:
    """Embed Factur-X XML and XMP metadata into an existing PDF.

    PDF/A-3 conformance also depends on the source PDF. Use :func:`validate_pdfa`
    or :func:`verify_facturx_round_trip` before distributing the result.
    """
    _require_facturx()
    pdf_bytes, xml_bytes = _bytes(source_pdf), _bytes(xml)
    if validate_xml:
        requested = None if level == "autodetect" else Profile(f"factur-x-{level}")
        report = validate_einvoice(xml_bytes, EInvoiceValidationOptions(profile=requested))
        if not report.valid:
            summary = "; ".join(f"{issue.code}: {issue.message}" for issue in report.errors[:5])
            raise ValueError(f"Factur-X XML is invalid: {summary}")
    from facturx import generate_from_binary

    return generate_from_binary(
        pdf_bytes,
        xml_bytes,
        flavor="factur-x",
        level=level,
        check_xsd=False,
        check_schematron=False,
        lang=lang,
        afrelationship="data",
    )


def extract_facturx_xml(source_pdf: Source) -> bytes:
    """Return the Factur-X XML attachment from a hybrid PDF."""
    _require_facturx()
    from facturx import get_xml_from_pdf

    xml = get_xml_from_pdf(_bytes(source_pdf), check_xsd=False, check_schematron=False)
    if isinstance(xml, tuple):
        _filename, xml = xml
    return xml.encode("utf-8") if isinstance(xml, str) else bytes(xml)


def _parse_verapdf(report: bytes, profile: str) -> PdfAValidationResult:
    try:
        root = ElementTree.fromstring(report)
    except ElementTree.ParseError as exc:
        raise FacturXPdfUnavailable(f"veraPDF returned an unreadable report: {exc}") from exc
    validation = root.find(".//validationReport")
    if validation is None:
        raise FacturXPdfUnavailable("veraPDF report has no validationReport")
    details = validation.find("details")
    attrs = details.attrib if details is not None else {}
    issues = []
    for rule in validation.findall(".//rule[@status='failed']"):
        issues.append(PdfAValidationIssue(
            specification=rule.get("specification"),
            clause=rule.get("clause"),
            test_number=rule.get("testNumber"),
            description=(rule.findtext("description") or "PDF/A rule failed").strip(),
            context=rule.findtext(".//check[@status='failed']/context"),
        ))
    version = root.find(".//releaseDetails[@id='core']")
    return PdfAValidationResult(
        compliant=validation.get("isCompliant") == "true",
        profile=validation.get("profileName") or profile,
        validator_version=version.get("version") if version is not None else None,
        passed_rules=int(attrs.get("passedRules", 0)),
        failed_rules=int(attrs.get("failedRules", 0)),
        passed_checks=int(attrs.get("passedChecks", 0)),
        failed_checks=int(attrs.get("failedChecks", 0)),
        issues=issues,
    )


def validate_pdfa(
    source_pdf: Source, *, executable: str = "verapdf", profile: Literal["3a", "3b", "3u"] = "3b"
) -> PdfAValidationResult:
    """Validate a PDF/A-3 container using the official veraPDF CLI."""
    command = shutil.which(executable) if Path(executable).name == executable else executable
    if not command or not Path(command).exists():
        raise FacturXPdfUnavailable(
            f"veraPDF executable {executable!r} was not found; install veraPDF or pass its path"
        )
    path: Path | None = None
    try:
        if isinstance(source_pdf, bytes):
            handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            handle.write(source_pdf)
            handle.close()
            path = Path(handle.name)
        else:
            path = Path(source_pdf)
        completed = subprocess.run(
            [str(command), "--format", "mrr", "--flavour", profile, str(path)],
            capture_output=True,
            check=False,
            timeout=120,
        )
        if not completed.stdout:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            raise FacturXPdfUnavailable(f"veraPDF failed ({completed.returncode}): {error}")
        return _parse_verapdf(completed.stdout, profile)
    except subprocess.TimeoutExpired as exc:
        raise FacturXPdfUnavailable("veraPDF timed out after 120 seconds") from exc
    finally:
        if isinstance(source_pdf, bytes) and path is not None:
            path.unlink(missing_ok=True)


def verify_facturx_round_trip(
    source_pdf: Source,
    *,
    expected_xml: Source | None = None,
    profile: Profile | None = None,
    verapdf: str = "verapdf",
) -> FacturXRoundTripResult:
    """Extract and validate both layers of a generated Factur-X PDF."""
    embedded = extract_facturx_xml(source_pdf)
    xml_report = validate_einvoice(embedded, EInvoiceValidationOptions(profile=profile))
    pdfa_report = validate_pdfa(source_pdf, executable=verapdf)
    matches = expected_xml is None or embedded == _bytes(expected_xml)
    return FacturXRoundTripResult(
        valid=matches and xml_report.valid and pdfa_report.compliant,
        xml_matches=matches,
        xml_validation=xml_report,
        pdfa_validation=pdfa_report,
    )


__all__ = [
    "FacturXPdfUnavailable", "FacturXRoundTripResult", "PdfAValidationIssue",
    "PdfAValidationResult", "extract_facturx_xml", "generate_facturx_pdf",
    "validate_pdfa", "verify_facturx_round_trip",
]
