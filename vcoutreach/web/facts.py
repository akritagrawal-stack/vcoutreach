"""Workspace approved facts shared by integration prompts and MIME checks."""
from pathlib import Path
from vcoutreach.web.config import Workspace,get_workspace

def load_facts(workspace=None):
    current = get_workspace() if workspace is None else workspace if isinstance(workspace,Workspace) else Workspace(Path(workspace))
    sender=current.read_json('sender.json',{}) or {}
    facts=current.read_json('facts.json',{}) or {}
    if not isinstance(sender,dict) or not isinstance(facts,dict):
        raise ValueError('Sender and facts files must contain JSON objects.')
    sender_claims=sender.get('approved_claims',{})
    claims=facts.get('approved_claims',{})
    if not isinstance(sender_claims,dict) or not isinstance(claims,dict):
        raise ValueError('Approved claims must be a JSON object.')
    forbidden=sender.get('forbidden_phrases',[])+facts.get('forbidden_phrases',[])
    if not all(isinstance(phrase,str) for phrase in forbidden):
        raise ValueError('Forbidden phrases must be strings.')
    return {**facts,'approved_claims':{**sender_claims,**claims},'forbidden_phrases':list(dict.fromkeys(forbidden))}

def approved_claims():
    return load_facts()['approved_claims']

def forbidden_phrases():
    return load_facts()['forbidden_phrases']
