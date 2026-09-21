"""A schema plugin: an EU single administrative document (customs declaration).

docket loads the `docket.schemas` entry point on first use of the catalog;
its target returns the SchemaSpecs to register (or registers them itself).
The model reuses docket's shared Party and Money blocks, so parties and
amounts serialize like everywhere else.
"""
from datetime import date

from pydantic import BaseModel, Field

from docket import CitedDocument, Money, Party, SchemaSpec, ValidationIssue, keywords


class DeclaredItem(BaseModel):
    description: str
    commodity_code: str = Field(description="8- or 10-digit TARIC/CN code as printed.")
    country_of_origin: str | None = None
    net_mass_kg: float | None = None
    statistical_value: Money | None = None


class CustomsDeclaration(CitedDocument):
    """Customs declaration (single administrative document, box numbers as printed)."""

    mrn: str = Field(description="Movement reference number (18 characters).")
    declaration_date: date
    declarant: Party
    consignee: Party
    procedure_code: str | None = Field(default=None, description="Box 37 procedure code, e.g. '4000'.")
    items: list[DeclaredItem] = Field(default_factory=list)


def mrn_format(declaration: CustomsDeclaration, ctx):
    if len(declaration.mrn.replace(" ", "")) != 18:
        yield ValidationIssue(field="mrn", message="an MRN has 18 characters")


def schemas() -> list[SchemaSpec]:
    return [
        SchemaSpec(
            schema_id="customs_declaration",
            version="1.0",
            display_name="Customs declaration",
            status="experimental",
            model=CustomsDeclaration,
            description="Customs declaration / single administrative document (SAD) / Zollanmeldung",
            keywords=keywords("single administrative document", "customs declaration", "zollanmeldung",
                              "déclaration en douane", "declaración aduanera", "dichiarazione doganale"),
            cited_fields=("mrn", "declaration_date", "declarant.name"),
            validators=(mrn_format,),
        )
    ]
