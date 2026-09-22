"""The pinned official sources of the e-invoice validation artifacts.

Each source is an upstream release archive pinned by URL and SHA-256, with
the files validation needs and where they go under a resource root.
`scripts/update_einvoice_resources.py` rebuilds the package's resources
from all of them; `fetch.py` downloads the ones Docket does not ship
(`redistributable: False`, see docs/THIRD_PARTY_LICENSES.md) on request.
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path

SOURCES = [
    {
        "id": "kosit-configuration",
        "name": "KoSIT validator configuration for XRechnung (OASIS UBL 2.1 XML Schemas)",
        "version": "XRechnung 3.0.2, release 2026-08-31",
        "url": "https://github.com/itplr-kosit/validator-configuration-xrechnung/releases/download/v2026-08-31/xrechnung-3.0.2-validator-configuration-2026-08-31.zip",
        "sha256": "2530cd107c414511c5d0462ec10f886910395abfca820db82e83d70bf01221a8",
        "license": "Apache-2.0 (configuration); OASIS UBL 2.1 notice",
        "license_files": ["licenses/Apache-2.0.txt", "licenses/OASIS-UBL-2.1.txt"],
        "redistributable": True,
        "files": {
            "resources/ubl/2.1/xsd/common/*.xsd": "ubl-2.1/xsd/common/",
            "resources/ubl/2.1/xsd/maindoc/UBL-Invoice-2.1.xsd": "ubl-2.1/xsd/maindoc/",
            "resources/ubl/2.1/xsd/maindoc/UBL-CreditNote-2.1.xsd": "ubl-2.1/xsd/maindoc/",
        },
    },
    {
        "id": "uncefact-cii-d16b",
        "name": "UN/CEFACT Cross Industry Invoice D16B XML Schemas, as distributed in the KoSIT XRechnung configuration",
        "version": "D16B (KoSIT release 2026-08-31)",
        "url": "https://github.com/itplr-kosit/validator-configuration-xrechnung/releases/download/v2026-08-31/xrechnung-3.0.2-validator-configuration-2026-08-31.zip",
        "sha256": "2530cd107c414511c5d0462ec10f886910395abfca820db82e83d70bf01221a8",
        "license": "UNKNOWN: no UN/CEFACT D16B redistribution grant located",
        "license_files": [],
        "redistributable": False,
        "files": {"resources/cii/16b/xsd/*.xsd": "cii-d16b/xsd/"},
    },
    {
        "id": "en16931-ubl",
        "name": "CEN EN 16931 validation artefacts, UBL syntax",
        "version": "1.3.16",
        "url": "https://github.com/ConnectingEurope/eInvoicing-EN16931/releases/download/validation-1.3.16/en16931-ubl-1.3.16.zip",
        "sha256": "bafada015efbc5248bf5e05ad2191e1d9833ef96e9dd5f4bce420a747342da85",
        "license": "EUPL-1.2",
        "license_files": ["licenses/EUPL-1.2.txt"],
        "redistributable": True,
        "files": {"xslt/EN16931-UBL-validation.xslt": "en16931/"},
        "fixtures": {"examples/*.xml": "en16931-ubl/", "examples/*.XML": "en16931-ubl/"},
    },
    {
        "id": "en16931-cii",
        "name": "CEN EN 16931 validation artefacts, UN/CEFACT CII syntax",
        "version": "1.3.16",
        "url": "https://github.com/ConnectingEurope/eInvoicing-EN16931/releases/download/validation-1.3.16/en16931-cii-1.3.16.zip",
        "sha256": "1cd53cb8a84d38aedc82c0caede217da983a7934dd663f793a092fd66443c561",
        "license": "EUPL-1.2",
        "license_files": ["licenses/EUPL-1.2.txt"],
        "redistributable": True,
        "files": {"xslt/EN16931-CII-validation.xslt": "en16931/"},
        "fixtures": {"examples/*.xml": "en16931-cii/"},
    },
    {
        "id": "xrechnung-schematron",
        "name": "KoSIT Schematron rules for CIUS XRechnung",
        "version": "XRechnung 3.0.2, schematron 2.6.0",
        "url": "https://github.com/itplr-kosit/xrechnung-schematron/releases/download/v2.6.0/xrechnung-3.0.2-schematron-2.6.0.zip",
        "sha256": "ca5e07afd04e72cd283d581590ffff3a15f4aac9d2f40c993345d78e67ea22b4",
        "license": "Apache-2.0",
        "license_files": ["licenses/Apache-2.0.txt"],
        "redistributable": True,
        "files": {
            "schematron/ubl/XRechnung-UBL-validation.xsl": "xrechnung/",
            "schematron/cii/XRechnung-CII-validation.xsl": "xrechnung/",
            "LICENSE": "xrechnung/",
        },
    },
    {
        "id": "xrechnung-testsuite",
        "name": "KoSIT XRechnung test suite (test fixtures only, not shipped)",
        "version": "XRechnung 3.0.2, release 2026-08-31",
        "url": "https://github.com/itplr-kosit/xrechnung-testsuite/releases/download/v2026-08-31/xrechnung-3.0.2-testsuite-2026-08-31.zip",
        "sha256": "1e81ef7563e0fa04c0e0b0631318a7e0843a3c0beb57d83ef95ac0c4a095e3d7",
        "license": "Apache-2.0",
        "redistributable": True,
        "files": {},
        "fixtures": {
            "instances/standard/01.01a-INVOICE_ubl.xml": "xrechnung/",
            "instances/standard/01.01a-INVOICE_uncefact.xml": "xrechnung/",
            "instances/standard/02.01a-INVOICE_ubl.xml": "xrechnung/",
            "instances/standard/02.01a-INVOICE_uncefact.xml": "xrechnung/",
        },
    },
    {
        "id": "peppol-bis-billing",
        "name": "OpenPeppol BIS Billing 3.0 Schematron",
        "version": "3.0.20",
        "url": "https://github.com/OpenPEPPOL/peppol-bis-invoice-3/archive/refs/tags/v3.0.20.zip",
        "sha256": "4c43040f0654abd789bb0c9a2ffbb9e795acd94f20282af8ca1db3dda832f656",
        "license": "UNKNOWN: no license or redistribution grant located in the v3.0.20 repository",
        "license_files": [],
        "redistributable": False,
        "files": {
            "peppol-bis-invoice-3-3.0.20/rules/sch/PEPPOL-EN16931-UBL.sch": "peppol/",
            "peppol-bis-invoice-3-3.0.20/rules/sch/PEPPOL-EN16931-CII.sch": "peppol/",
        },
        "compile": ["peppol/PEPPOL-EN16931-UBL.sch", "peppol/PEPPOL-EN16931-CII.sch"],
        "fixtures": {
            "peppol-bis-invoice-3-3.0.20/rules/examples/*.xml": "peppol/examples/",
            "peppol-bis-invoice-3-3.0.20/rules/unit-UBL-PEPPOL/*.xml": "peppol/unit-UBL-PEPPOL/",
        },
    },
    {
        "id": "factur-x",
        "name": "Factur-X 1.09 / ZUGFeRD XML Schemas and Schematron per profile (FNFE-MPE and FeRD), as redistributed in the factur-x 6.8 Python package",
        "version": "Factur-X 1.09 (factur-x 6.8)",
        "url": "https://files.pythonhosted.org/packages/37/be/9a9020187e4805d61668b5d8c4c263731b3697f9eba2aa6a689d3e7eea4b/factur_x-6.8-py3-none-any.whl",
        "sha256": "02b57dd57f59d0cdd87f034a538bf30a4b9241a72a19cd75fe1538233163ee15",
        "license": "UNKNOWN: FNFE-MPE / FeRD artefact redistribution terms not located; source package is BSD-3-Clause",
        "license_files": [],
        "redistributable": False,
        "files": {
            f"facturx/xsd_and_schematron/facturx-{profile}/*": f"factur-x/{profile}/"
            for profile in ("minimum", "basicwl", "basic", "en16931", "extended")
        },
    },
]

SCHXSLT = {
    "id": "schxslt",
    "name": "SchXslt Schematron-to-XSLT compiler (used to compile the Peppol rules; not shipped)",
    "version": "1.10.1",
    "url": "https://repo1.maven.org/maven2/name/dmaus/schxslt/schxslt/1.10.1/schxslt-1.10.1.jar",
    "sha256": "4f4f21edab7b37f96ad59ae12a344d3510f1092ac46b6d81a4efa0120b73cb58",
    "license": "MIT",
}


class SourceError(RuntimeError):
    """A pinned source could not be downloaded or does not match its pin."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(source: dict, cache: Path | None = None, timeout: float = 120) -> bytes:
    """The source archive, checked against its pinned SHA-256. With `cache`,
    a verified earlier download is reused."""
    target = cache / Path(source["url"]).name if cache else None
    if target is not None and target.exists():
        data = target.read_bytes()
    else:
        try:
            with urllib.request.urlopen(source["url"], timeout=timeout) as response:
                data = response.read()
        except OSError as exc:
            raise SourceError(f"{source['id']}: cannot download {source['url']}: {exc}") from exc
    digest = sha256(data)
    if digest != source["sha256"]:
        raise SourceError(f"{source['id']}: sha256 {digest} does not match the pinned {source['sha256']}")
    if target is not None and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return data


def extract(archive: zipfile.ZipFile, mapping: dict[str, str], dest_root: Path) -> list[Path]:
    written = []
    names = archive.namelist()
    for pattern, dest in mapping.items():
        matches = [n for n in names if fnmatch.fnmatch(n, pattern) and not n.endswith("/")]
        if not matches:
            raise SourceError(f"nothing matches {pattern!r}")
        for name in matches:
            target = dest_root / dest / Path(name).name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
            written.append(target)
    return written


def compile_schematron(sch: Path, schxslt_jar: bytes) -> Path:
    """Compile a published .sch to the XSLT the validator runs, next to it."""
    from saxonche import PySaxonProcessor

    work = sch.parent / ".schxslt"
    shutil.rmtree(work, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(schxslt_jar)) as jar:
        jar.extractall(work, members=[n for n in jar.namelist() if n.startswith("xslt/")])
    try:
        with PySaxonProcessor(license=False) as processor:
            xslt = processor.new_xslt30_processor()
            compiler = xslt.compile_stylesheet(stylesheet_file=str(work / "xslt/2.0/pipeline-for-svrl.xsl"))
            output = compiler.transform_to_string(source_file=str(sch))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    # SchXslt stamps the compile time; drop it so the output is reproducible
    # and a download can be checked against the manifest.
    output = re.sub(r"<dct:created>[^<]*</dct:created>", "<dct:created/>", output)
    target = sch.with_suffix(".xsl")
    target.write_text(output, encoding="utf-8")
    return target


def install(source: dict, dest_root: Path, cache: Path | None = None,
            fixtures_root: Path | None = None) -> list[Path]:
    """Extract one source's files (and compile its Schematron) under
    `dest_root`; the paths written, relative layout as in the manifest."""
    data = download(source, cache)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = extract(archive, source["files"], dest_root)
        if fixtures_root is not None:
            extract(archive, source.get("fixtures", {}), fixtures_root)
    if source.get("compile"):
        schxslt_jar = download(SCHXSLT, cache)
        files.extend(compile_schematron(dest_root / sch, schxslt_jar) for sch in source["compile"])
    return files


__all__ = ["SCHXSLT", "SOURCES", "SourceError", "compile_schematron", "download", "extract", "install", "sha256"]
