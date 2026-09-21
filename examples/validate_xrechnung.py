"""Check an XRechnung (UBL or CII) with the KoSIT and CEN rules, offline.

    pip install "docket-idp[einvoice]"
    python examples/validate_xrechnung.py rechnung.xml

The same check on the command line: `docket validate-einvoice rechnung.xml --profile xrechnung`.
"""
import sys

from docket import EInvoiceProfile, EInvoiceValidationOptions, validate_einvoice

report = validate_einvoice(sys.argv[1], EInvoiceValidationOptions(profile=EInvoiceProfile.XRECHNUNG))

print(f"{'valid' if report.valid else 'INVALID'}: {report.detected_format}, declares {report.declared_profile_id}")
for layer in report.layers:
    status = "skipped" if not layer.ran else ("passed" if layer.passed else "failed")
    print(f"  {layer.layer:10} {layer.artifact} ({layer.version}): {status}")
for issue in report.issues:
    # BR-DE-* are the German CIUS rules (e.g. BR-DE-15: Leitweg-ID / buyer reference missing),
    # BR-* the EN 16931 core, DOCKET-PROFILE-MISMATCH a document that isn't declared as XRechnung.
    print(f"  [{issue.severity}] {issue.code} ({issue.layer}): {issue.message}")
    if issue.location:
        print(f"      at {issue.location}")
sys.exit(0 if report.valid else 2)
