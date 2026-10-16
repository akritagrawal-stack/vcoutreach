"""Load editable boilerplate or derive it from the workspace sender profile."""
from vcoutreach.web.config import get_workspace

REQUIRED_KEYS = {'version', 'intro', 'product_para', 'traction_para', 'cta', 'signatures'}
V2_REQUIRED_KEYS = {'version', 'audience', 'intro', 'value_para', 'proof_para', 'cta', 'signatures'}

def get_boilerplate(audience: str | None = None) -> dict:
    workspace = get_workspace()
    if audience not in {None, 'vc', 'startup'}:
        raise ValueError('Unknown boilerplate audience.')
    name = 'boilerplate.json' if audience is None else f'boilerplate_v2_{audience}.json'
    data = workspace.read_json('data/' + name)
    if isinstance(data, dict):
        return data
    sender = workspace.read_json('sender.json', {}) or {}
    product = '\n\n'.join(sender.get('product_lines', []))
    traction = '\n\n'.join(sender.get('traction_lines', []))
    return {'version': 'workspace-1', 'audience': audience or 'vc', 'intro': '',
            'product_para': product, 'value_para': product, 'traction_para': traction,
            'proof_para': traction, 'cta': sender.get('ask_line', ''),
            'signatures': {'sender': sender.get('signature') or sender.get('name', '')}}

def reload_boilerplate() -> dict:
    return get_boilerplate()

def get_version() -> str:
    return get_boilerplate()['version']
