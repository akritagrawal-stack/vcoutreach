"""Legacy importer entrypoints for user-supplied contact CSV files."""
from pathlib import Path
from typing import Any
from vcoutreach.contacts import load_contacts

def _derive_first_name(partner_name: str) -> str:
    return partner_name.strip().split()[0] if partner_name.strip() else ''

def _parse_name_firm(line: str) -> tuple[str, str]:
    name, _, firm = line.partition(',')
    return name.strip(), firm.strip()

def parse_docx(docx_path: Path | None = None) -> list[dict[str, Any]]:
    if docx_path is None:
        return []
    return [dict(partner_name=c.name,first_name=c.first_name,firm=c.org,email=c.email) for c in load_contacts(docx_path)]

def dry_run(docx_path: Path | None = None) -> None:
    print(f'{len(parse_docx(docx_path))} contacts ready to import.')

def run_import(docx_path: Path | None = None, db_path: Path | None = None) -> None:
    if docx_path is None:
        return
    from vcoutreach.web.drafts_service import import_contacts
    import_contacts(load_contacts(docx_path),db_path=db_path)
