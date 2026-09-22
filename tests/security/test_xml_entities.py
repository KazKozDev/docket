import pytest

from docket import validate_einvoice

pytest.importorskip("lxml")
pytest.importorskip("saxonche")


@pytest.mark.parametrize(
    "doctype",
    [
        '<!DOCTYPE Invoice [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>',
        '<!DOCTYPE Invoice [<!ENTITY a "1234567890"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>',
    ],
)
def test_dtd_external_entities_and_expansion_are_refused(doctype):
    xml = (
        doctype
        + '<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2">&xxe;</Invoice>'
    ).replace("&xxe;", "&b;" if "ENTITY b" in doctype else "&xxe;")
    result = validate_einvoice(xml.encode())
    assert not result.valid
    assert {issue.code for issue in result.issues} == {"DOCKET-XML"}
    assert "root:" not in result.model_dump_json()
