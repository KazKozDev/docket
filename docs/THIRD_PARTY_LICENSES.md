# Third-party e-invoice artefacts

This inventory covers the validation resources recorded in
`src/docket/einvoice/resources/manifest.json`. It was reviewed on
2026-09-22 against the upstream locations linked below. It is an engineering
compliance record, not legal advice. "Unclear" means the upstream source did
not provide a redistribution grant that could be verified; it does not mean
that redistribution is forbidden.

| Artefact | Version and source | Licence evidence | Conditions | Distribution status |
|---|---|---|---|---|
| KoSIT XRechnung configuration | XRechnung 3.0.2, [release 2026-08-31](https://github.com/itplr-kosit/validator-configuration-xrechnung/releases/tag/v2026-08-31) | The [upstream repository](https://github.com/itplr-kosit/validator-configuration-xrechnung) and its `LICENSE` identify Apache-2.0. | Include the Apache-2.0 text, retain notices, and mark modifications. The upstream `LICENSE` also preserves the CEN/DIN XRechnung notice. | May be redistributed under the stated terms. |
| OASIS UBL 2.1 schemas inside the KoSIT bundle | OASIS Standard, [4 November 2013](https://docs.oasis-open.org/ubl/os-UBL-2.1/) | Each shipped schema contains the OASIS copyright and permission notice. | Copying and implementation-oriented derivative material are permitted when the copyright notice and permission paragraph are retained. The schemas are shipped unmodified with their notices; a copy is also in `licenses/OASIS-UBL-2.1.txt`. | May be redistributed with the notice retained. |
| UN/CEFACT CII D16B schemas inside the KoSIT bundle | D16B, obtained from the pinned KoSIT release; [UNECE schema catalogue](https://unece.org/fileadmin/DAM/uncefact/xml_schemas/index.htm) | UNECE publishes the schemas for download, but a D16B-specific redistribution licence or notice was not found in the archive or catalogue during this review. Newer UN/CEFACT pages use CC BY 4.0, but that was not treated as retroactive evidence for D16B. | Unknown. Preserve provenance and do not imply UNECE endorsement. | **Unclear; upstream confirmation required.** |
| CEN/TC 434 EN 16931 UBL and CII validation artefacts | 1.3.16, [upstream repository](https://github.com/ConnectingEurope/eInvoicing-EN16931) | Upstream source files and Maven metadata state EUPL-1.2. | Include the EUPL-1.2 text and notices. Provide the source or a durable source location. Modified files require a modification notice. Docket ships the generated XSLT unchanged and links the pinned source archive in the manifest. | May be redistributed under EUPL-1.2 obligations. |
| KoSIT XRechnung Schematron | XRechnung 3.0.2 / Schematron 2.6.0, [release](https://github.com/itplr-kosit/xrechnung-schematron/releases/tag/v2.6.0) | Upstream `LICENSE` is Apache-2.0 and is included in the pinned archive. | Include Apache-2.0 and retain notices. | May be redistributed under the stated terms. |
| KoSIT XRechnung test suite | XRechnung 3.0.2, release 2026-08-31, [upstream repository](https://github.com/itplr-kosit/xrechnung-testsuite) | Upstream identifies Apache-2.0. | Test fixtures live in the source distribution only and are not package resources. Retain the upstream notices when redistributing the repository. | May be redistributed under the stated terms; not shipped in the wheel. |
| OpenPeppol BIS Billing rules | 3.0.20, [tag v3.0.20](https://github.com/OpenPEPPOL/peppol-bis-invoice-3/tree/v3.0.20) | No `LICENSE`, per-file grant, or redistribution terms were found in the tagged repository. Publication for implementers is evidence of availability, not a redistribution licence. | Unknown. | **Problem: redistribution permission is unverified.** |
| Factur-X schemas and Schematron | Factur-X 1.09 from [`factur-x` 6.8](https://pypi.org/project/factur-x/6.8/) | The Python distribution is BSD-3-Clause. Its bundled standard artefacts are attributed to FNFE-MPE and FeRD, but no separate FNFE/FeRD redistribution terms were found in the wheel or the form-gated official download. The package licence alone is not assumed to relicense third-party standards. | BSD-3-Clause notice is included for the source package. Conditions for the standard artefacts remain unknown. | **Problem: FNFE/FeRD redistribution permission is unverified.** |
| SchXslt compiler | 1.10.1, [Maven Central](https://repo1.maven.org/maven2/name/dmaus/schxslt/schxslt/1.10.1/) | MIT according to upstream metadata. | Build-time compiler only; the JAR is not shipped. Generated Peppol XSLT remains subject to the unresolved Peppol terms. | Not distributed. |

## Open issues

Before representing the base wheel as fully cleared for third-party artefact
redistribution, obtain written or published terms for OpenPeppol 3.0.20,
Factur-X 1.09 from FNFE-MPE/FeRD, and UN/CEFACT CII D16B. No vendored file has
been removed while those questions are investigated.

If permission cannot be confirmed, the practical packaging options are:

1. Download the affected artefacts after an explicit user action and verify
   them against the pinned SHA-256 values.
2. Move affected validators to a separately installed extra that downloads or
   accepts a user-provided resource directory.
3. Ship only artefacts with verified redistribution terms and return a clear
   configuration error for unavailable profiles.

The manifest is the machine-readable source of truth. Every entry has a
non-empty `license` assessment; `license_files` names each notice that must be
present in the installed resources. The update script preserves these reviewed
texts when rebuilding the pinned artefacts.
