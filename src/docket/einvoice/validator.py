"""Validate e-invoices against the official XML Schemas and Schematron rules.

    from docket import validate_einvoice

    report = validate_einvoice("invoice.xml")            # profile read from the XML
    report = validate_einvoice(xml_bytes, EInvoiceValidationOptions(profile="xrechnung"))
    report.valid, report.profile, report.issues

Two layers, in order, each with the official artifact for the profile:

1. **XSD** (lxml): OASIS UBL 2.1 Invoice/CreditNote, UN/CEFACT CII D16B, or
   the Factur-X 1.09 schema of the profile.
2. **Schematron** (SaxonC-HE running the published XSLT): CEN EN 16931,
   then the CIUS on top — Peppol BIS Billing 3.0 or XRechnung 3.0 — or the
   Factur-X profile's own rules. Skipped when the XSD layer fails, as the
   KoSIT validator does: business rules over a document that isn't the
   right shape report noise.

The profile the document declares (UBL `CustomizationID`, CII guideline
ID) picks the rules. When the caller asks for a profile, a document that
declares a different one is reported (`DOCKET-PROFILE-MISMATCH`) and the
requested profile's rules run. A Factur-X / ZUGFeRD PDF is accepted: its
embedded XML is validated.

Needs `pip install "docket-idp[einvoice]"` (lxml, saxonche). Everything runs
offline from the vendored artifacts (see artifacts.py).
"""
from __future__ import annotations

import importlib.util
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .. import __version__
from ..errors import ConfigurationError
from . import artifacts

UBL_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
UBL_CREDIT_NOTE = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
CII = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
_NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "rsm": CII,
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "svrl": "http://purl.oclc.org/dsdl/svrl",
}
INSTALL_HINT = 'e-invoice validation needs `pip install "docket-idp[einvoice]"`'


class Profile(str, Enum):
    EN16931 = "en16931"
    PEPPOL = "peppol"
    XRECHNUNG = "xrechnung"
    FACTURX_MINIMUM = "factur-x-minimum"
    FACTURX_BASICWL = "factur-x-basicwl"
    FACTURX_BASIC = "factur-x-basic"
    FACTURX_EN16931 = "factur-x-en16931"
    FACTURX_EXTENDED = "factur-x-extended"
    FACTURX_XRECHNUNG = "factur-x-xrechnung"


# The specification identifier each profile declares (BT-24).
PROFILE_IDS: dict[Profile, str] = {
    Profile.EN16931: "urn:cen.eu:en16931:2017",
    Profile.PEPPOL: "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0",
    Profile.XRECHNUNG: "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0",
    Profile.FACTURX_MINIMUM: "urn:factur-x.eu:1p0:minimum",
    Profile.FACTURX_BASICWL: "urn:factur-x.eu:1p0:basicwl",
    Profile.FACTURX_BASIC: "urn:cen.eu:en16931:2017#compliant#urn:factur-x.eu:1p0:basic",
    Profile.FACTURX_EN16931: "urn:cen.eu:en16931:2017",
    Profile.FACTURX_EXTENDED: "urn:cen.eu:en16931:2017#conformant#urn:factur-x.eu:1p0:extended",
    Profile.FACTURX_XRECHNUNG: "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0",
}
_XRECHNUNG_EXTENSION = "urn:cen.eu:en16931:2017#conformant#urn:xeinkauf.de:kosit:extension:xrechnung_3.0"

Syntax = Literal["ubl", "cii"]


class EInvoiceIssue(BaseModel):
    code: str = Field(description="Rule id (BR-CO-15, PEPPOL-EN16931-R001, BR-DE-1) or DOCKET-/XSD- code.")
    severity: Literal["fatal", "error", "warning", "info"]
    message: str
    location: str | None = Field(default=None, description="XPath (Schematron) or line and path (XSD).")
    rule_source: str = Field(description="Artifact and version the rule comes from.")
    layer: Literal["input", "profile", "xsd", "schematron"]


class LayerReport(BaseModel):
    layer: Literal["xsd", "schematron"]
    artifact: str
    version: str
    ran: bool
    passed: bool | None = None
    rules_fired: int | None = None
    skipped_reason: str | None = None


class EInvoiceValidationResult(BaseModel):
    valid: bool
    detected_format: str | None = Field(
        default=None, description="ubl-invoice, ubl-credit-note, cii; prefixed factur-x-pdf/ when read from a PDF."
    )
    syntax: Syntax | None = None
    profile: Profile | None = Field(default=None, description="Profile whose rules ran.")
    declared_profile_id: str | None = Field(default=None, description="Specification identifier in the document.")
    validator: str = "docket e-invoice validator (lxml XSD + SaxonC-HE Schematron)"
    validator_version: str = ""
    validation_resource_version: str = ""
    layers: list[LayerReport] = Field(default_factory=list)
    issues: list[EInvoiceIssue] = Field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def errors(self) -> list[EInvoiceIssue]:
        return [i for i in self.issues if i.severity in ("fatal", "error")]


class EInvoiceValidationOptions(BaseModel):
    profile: Profile | None = Field(
        default=None, description="Validate against this profile; default: the one the document declares."
    )


class EInvoiceUnavailable(ConfigurationError):
    """The [einvoice] extra is not installed."""


# ---- plans ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Rules:
    artifact: str
    file: str


@dataclass(frozen=True)
class _Plan:
    xsd_artifact: str
    xsd: dict[str, str]  # document kind -> schema file
    rules: tuple[_Rules, ...]


_UBL_XSD = {"invoice": "ubl-2.1/xsd/maindoc/UBL-Invoice-2.1.xsd", "credit-note": "ubl-2.1/xsd/maindoc/UBL-CreditNote-2.1.xsd"}
_CII_XSD = {"invoice": "cii-d16b/xsd/CrossIndustryInvoice_100pD16B.xsd"}
_EN_UBL = _Rules("en16931-ubl", "en16931/EN16931-UBL-validation.xslt")
_EN_CII = _Rules("en16931-cii", "en16931/EN16931-CII-validation.xslt")


def _facturx(profile: str, upper: str) -> _Plan:
    return _Plan(
        "factur-x",
        {"invoice": f"factur-x/{profile}/Factur-X_{upper}.xsd"},
        (_Rules("factur-x", f"factur-x/{profile}/Factur-X_1.09_{upper}.xsl"),),
    )


_PLANS: dict[tuple[str, Profile], _Plan] = {
    ("ubl", Profile.EN16931): _Plan("kosit-configuration", _UBL_XSD, (_EN_UBL,)),
    ("ubl", Profile.PEPPOL): _Plan(
        "kosit-configuration", _UBL_XSD, (_EN_UBL, _Rules("peppol-bis-billing", "peppol/PEPPOL-EN16931-UBL.xsl"))
    ),
    ("ubl", Profile.XRECHNUNG): _Plan(
        "kosit-configuration", _UBL_XSD, (_EN_UBL, _Rules("xrechnung-schematron", "xrechnung/XRechnung-UBL-validation.xsl"))
    ),
    ("cii", Profile.EN16931): _Plan("kosit-configuration", _CII_XSD, (_EN_CII,)),
    ("cii", Profile.PEPPOL): _Plan(
        "kosit-configuration", _CII_XSD, (_EN_CII, _Rules("peppol-bis-billing", "peppol/PEPPOL-EN16931-CII.xsl"))
    ),
    ("cii", Profile.FACTURX_XRECHNUNG): _Plan(
        "kosit-configuration", _CII_XSD, (_EN_CII, _Rules("xrechnung-schematron", "xrechnung/XRechnung-CII-validation.xsl"))
    ),
    ("cii", Profile.FACTURX_MINIMUM): _facturx("minimum", "MINIMUM"),
    ("cii", Profile.FACTURX_BASICWL): _facturx("basicwl", "BASICWL"),
    ("cii", Profile.FACTURX_BASIC): _facturx("basic", "BASIC"),
    ("cii", Profile.FACTURX_EN16931): _facturx("en16931", "EN16931"),
    ("cii", Profile.FACTURX_EXTENDED): _facturx("extended", "EXTENDED"),
}
# The same request under the other syntax's name.
_ALIASES = {
    ("cii", Profile.XRECHNUNG): Profile.FACTURX_XRECHNUNG,
    ("ubl", Profile.FACTURX_XRECHNUNG): Profile.XRECHNUNG,
}


def _declared_profile(syntax: str, spec_id: str | None) -> Profile | None:
    if not spec_id:
        return None
    spec_id = spec_id.strip()
    if spec_id == _XRECHNUNG_EXTENSION:
        return Profile.XRECHNUNG if syntax == "ubl" else Profile.FACTURX_XRECHNUNG
    if syntax == "cii":
        if spec_id == PROFILE_IDS[Profile.EN16931]:
            return Profile.FACTURX_EN16931
        if spec_id == PROFILE_IDS[Profile.XRECHNUNG]:
            return Profile.FACTURX_XRECHNUNG
        if spec_id == PROFILE_IDS[Profile.PEPPOL]:
            return Profile.PEPPOL
        candidates = [p for p in Profile if p.value.startswith("factur-x")]
    else:
        candidates = [Profile.EN16931, Profile.PEPPOL, Profile.XRECHNUNG]
    return next((p for p in candidates if PROFILE_IDS[p] == spec_id), None)


# ---- engines ------------------------------------------------------------------------


def available() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("lxml", "saxonche"))


def _require() -> None:
    if not available():
        raise EInvoiceUnavailable(INSTALL_HINT)


class _Engine:
    """One Saxon processor per process; compiled stylesheets and schemas are
    cached. Saxon's Python binding is not documented as thread-safe, so
    transforms are serialized."""

    def __init__(self) -> None:
        from saxonche import PySaxonProcessor

        self._processor = PySaxonProcessor(license=False)
        self._xslt = self._processor.new_xslt30_processor()
        self._lock = threading.Lock()
        self._stylesheets: dict[str, object] = {}
        self._schemas: dict[str, object] = {}

    @property
    def saxon_version(self) -> str:
        return self._processor.version

    def schema(self, relative: str):
        from lxml import etree

        with self._lock:
            if relative not in self._schemas:
                self._schemas[relative] = etree.XMLSchema(etree.parse(str(artifacts.path(relative))))
            return self._schemas[relative]

    def svrl(self, relative: str, document: bytes) -> bytes:
        with self._lock:
            executable = self._stylesheets.get(relative)
            if executable is None:
                executable = self._xslt.compile_stylesheet(stylesheet_file=str(artifacts.path(relative)))
                self._stylesheets[relative] = executable
            node = self._processor.parse_xml(xml_text=document.decode("utf-8"))
            return executable.transform_to_string(xdm_node=node).encode("utf-8")


_engine: _Engine | None = None
_engine_lock = threading.Lock()


def _get_engine() -> _Engine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _require()
            _engine = _Engine()
        return _engine


# ---- input ------------------------------------------------------------------------------


_ATTACHMENT_NAMES = ("factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml", "zugferd.xml")


def _read(source: str | Path | bytes) -> tuple[bytes, str | None]:
    """(XML bytes, container) — container is 'pdf' when XML came out of a PDF."""
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    if data[:5] != b"%PDF-":
        return data, None
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise ValueError(f"unreadable PDF: {exc}") from exc
    try:
        found = {}
        for i in range(pdf.count_attachments()):
            attachment = pdf.get_attachment(i)
            found[attachment.get_name().lower()] = bytes(attachment.get_data())
    finally:
        pdf.close()
    for name in _ATTACHMENT_NAMES:
        if name in found:
            return found[name], "pdf"
    raise ValueError(
        f"the PDF embeds no e-invoice XML (looked for {', '.join(_ATTACHMENT_NAMES)}; found {sorted(found) or 'none'})"
    )


def _parse_svrl(svrl: bytes, source: str) -> tuple[list[EInvoiceIssue], int]:
    from lxml import etree

    root = etree.fromstring(svrl)
    issues = []
    for tag, default in (("failed-assert", "error"), ("successful-report", "warning")):
        for node in root.iter(f"{{{_NS['svrl']}}}{tag}"):
            flag = (node.get("flag") or node.get("role") or default).lower()
            severity = {"fatal": "fatal", "error": "error", "warning": "warning", "information": "info", "info": "info"}.get(
                flag, "error"
            )
            text = " ".join("".join(node.itertext()).split())
            issues.append(
                EInvoiceIssue(
                    code=node.get("id") or "SCHEMATRON",
                    severity=severity,
                    message=text,
                    location=node.get("location"),
                    rule_source=source,
                    layer="schematron",
                )
            )
    fired = sum(1 for _ in root.iter(f"{{{_NS['svrl']}}}fired-rule"))
    return issues, fired


def _version(artifact_id: str) -> str:
    entry = artifacts.artifact(artifact_id)
    return f"{entry['name']} {entry['version']}"


# ---- validation -----------------------------------------------------------------------------


def validate_einvoice(
    source: str | Path | bytes, options: EInvoiceValidationOptions | None = None
) -> EInvoiceValidationResult:
    """Validate one e-invoice (XML, or a Factur-X/ZUGFeRD PDF) with the
    official artifacts of its profile."""
    _require()
    from lxml import etree

    options = options or EInvoiceValidationOptions()
    started = time.monotonic()
    engine = _get_engine()
    import lxml

    result = EInvoiceValidationResult(
        valid=False,
        validator_version=f"docket {__version__}; lxml {lxml.__version__}; {engine.saxon_version}",
    )

    def finish(**updates) -> EInvoiceValidationResult:
        final = result.model_copy(update=updates)
        valid = not final.errors and bool(final.layers) and all(l.ran and l.passed for l in final.layers)
        return final.model_copy(
            update={"valid": valid, "elapsed_seconds": round(time.monotonic() - started, 3)}
        )

    try:
        xml, container = _read(source)
    except (OSError, ValueError) as exc:
        return finish(issues=[EInvoiceIssue(code="DOCKET-INPUT", severity="fatal", message=str(exc),
                                            rule_source="docket", layer="input")])
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        tree = etree.fromstring(xml, parser)
    except etree.XMLSyntaxError as exc:
        return finish(issues=[EInvoiceIssue(code="DOCKET-XML", severity="fatal", message=f"not well-formed XML: {exc}",
                                            rule_source="docket", layer="input")])

    if tree.getroottree().docinfo.internalDTD is not None:
        # No e-invoice standard uses a DTD; entities could reach local files.
        return finish(issues=[EInvoiceIssue(code="DOCKET-XML", severity="fatal", rule_source="docket", layer="input",
                                            message="documents with a DOCTYPE declaration are refused")])
    namespace = etree.QName(tree).namespace
    if namespace == UBL_INVOICE:
        syntax, kind, detected = "ubl", "invoice", "ubl-invoice"
        spec_id = tree.findtext("cbc:CustomizationID", namespaces=_NS)
    elif namespace == UBL_CREDIT_NOTE:
        syntax, kind, detected = "ubl", "credit-note", "ubl-credit-note"
        spec_id = tree.findtext("cbc:CustomizationID", namespaces=_NS)
    elif namespace == CII:
        syntax, kind, detected = "cii", "invoice", "cii"
        spec_id = tree.findtext(
            "rsm:ExchangedDocumentContext/ram:GuidelineSpecifiedDocumentContextParameter/ram:ID", namespaces=_NS
        )
    else:
        return finish(issues=[EInvoiceIssue(
            code="DOCKET-FORMAT", severity="fatal", rule_source="docket", layer="input",
            message=f"root element {tree.tag} is neither a UBL Invoice/CreditNote nor a CII CrossIndustryInvoice",
        )])
    if container:
        detected = f"factur-x-pdf/{detected}"
    result = result.model_copy(update={"detected_format": detected, "syntax": syntax,
                                       "declared_profile_id": spec_id})
    issues: list[EInvoiceIssue] = []

    declared = _declared_profile(syntax, spec_id)
    requested = options.profile
    if requested is not None:
        requested = _ALIASES.get((syntax, requested), requested)
    profile = requested or declared
    if profile is None:
        return finish(issues=[EInvoiceIssue(
            code="DOCKET-PROFILE-UNKNOWN", severity="fatal", rule_source="docket", layer="profile",
            message=f"the document declares {spec_id!r}, which is no supported profile; pass a profile to validate against",
        )])
    if requested is not None and declared != requested:
        issues.append(EInvoiceIssue(
            code="DOCKET-PROFILE-MISMATCH", severity="error", rule_source="docket", layer="profile",
            location="/*/cbc:CustomizationID" if syntax == "ubl"
            else "/rsm:CrossIndustryInvoice/rsm:ExchangedDocumentContext/ram:GuidelineSpecifiedDocumentContextParameter/ram:ID",
            message=f"validating as {requested.value} ({PROFILE_IDS[requested]}), but the document declares {spec_id!r}",
        ))
    plan = _PLANS.get((syntax, profile))
    if plan is None:
        return finish(profile=profile, issues=issues + [EInvoiceIssue(
            code="DOCKET-PROFILE-SYNTAX", severity="fatal", rule_source="docket", layer="profile",
            message=f"profile {profile.value} is not defined for {syntax.upper()} documents",
        )])
    if kind not in plan.xsd:
        return finish(profile=profile, issues=issues + [EInvoiceIssue(
            code="DOCKET-DOCUMENT-KIND", severity="fatal", rule_source="docket", layer="profile",
            message=f"profile {profile.value} has no schema for a {kind}",
        )])

    layers: list[LayerReport] = []
    xsd_source = _version(plan.xsd_artifact)
    schema = engine.schema(plan.xsd[kind])
    xsd_ok = schema.validate(tree)
    for entry in schema.error_log:
        issues.append(EInvoiceIssue(
            code="XSD", severity="fatal", message=entry.message, location=f"line {entry.line}: {entry.path}",
            rule_source=xsd_source, layer="xsd",
        ))
    layers.append(LayerReport(layer="xsd", artifact=plan.xsd[kind], version=xsd_source, ran=True, passed=xsd_ok))

    for rules in plan.rules:
        source = _version(rules.artifact)
        if not xsd_ok:
            layers.append(LayerReport(layer="schematron", artifact=rules.file, version=source, ran=False,
                                      skipped_reason="XML Schema validation failed"))
            continue
        found, fired = _parse_svrl(engine.svrl(rules.file, xml), source)
        issues.extend(found)
        layers.append(LayerReport(
            layer="schematron", artifact=rules.file, version=source, ran=True, rules_fired=fired,
            passed=not any(i.severity in ("fatal", "error") for i in found),
        ))

    used = {plan.xsd_artifact} | {r.artifact for r in plan.rules}
    return finish(
        profile=profile,
        layers=layers,
        issues=issues,
        validation_resource_version="; ".join(sorted(_version(a) for a in used)),
    )


__all__ = [
    "EInvoiceIssue",
    "EInvoiceUnavailable",
    "EInvoiceValidationOptions",
    "EInvoiceValidationResult",
    "LayerReport",
    "PROFILE_IDS",
    "Profile",
    "available",
    "validate_einvoice",
]
