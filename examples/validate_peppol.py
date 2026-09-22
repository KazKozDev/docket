"""Check UBL invoices against Peppol BIS Billing 3.0 (or plain EN 16931).

    pip install "docket-idp[einvoice]"
    docket einvoice fetch        # once: the Peppol rules are not shipped
    python examples/validate_peppol.py invoices/*.xml
    python examples/validate_peppol.py --en16931 invoice.xml    # core rules only

Prints one JSON line per file, handy for CI or a pre-send hook.
"""
import json
import sys

from docket import EInvoiceProfile, EInvoiceValidationOptions, validate_einvoice

args = sys.argv[1:]
profile = EInvoiceProfile.EN16931 if "--en16931" in args else EInvoiceProfile.PEPPOL
files = [a for a in args if not a.startswith("--")]

failed = 0
for path in files:
    report = validate_einvoice(path, EInvoiceValidationOptions(profile=profile))
    failed += not report.valid
    print(json.dumps({
        "file": path,
        "valid": report.valid,
        "profile": report.profile.value if report.profile else None,
        "rules": report.validation_resource_version,
        # PEPPOL-EN16931-* are Peppol's own rules, e.g. CL008 for an endpoint scheme outside the EAS list.
        "errors": [
            {"code": i.code, "layer": i.layer, "location": i.location, "message": i.message}
            for i in report.issues if i.severity in ("fatal", "error")
        ],
    }, ensure_ascii=False))
sys.exit(2 if failed else 0)
