"""Generic campaign summary from local contacts and drafts."""
from vcoutreach.web.db import get_conn

def hash_email(value: str | None):
    from vcoutreach.web.vendor import email_identity_hash
    return email_identity_hash(value) if value else ''

def load_publik_campaign():
    conn = get_conn()
    try:
        records = [dict(row) for row in conn.execute('SELECT * FROM partners ORDER BY id')]
    finally:
        conn.close()
    return {'records': records, 'email_hashes': set(), 'total': len(records),
            'filtered_count': len(records), 'sent': 0, 'reviewed': 0, 'cohorts': []}

def filter_publik_replies(rows: list[dict], email_hashes: set[str]):
    return list(rows)
