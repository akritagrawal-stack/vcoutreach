"""Offline provider responses through the documented request_fn seam."""
import pytest

from vcoutreach.web.enrichment import contact_provider_clients as clients


CONTACT = {"id": 1, "partner_name": "Ada Lovelace", "name": "Ada Lovelace", "firm": "Northwind Labs",
           "org": "Northwind Labs", "domain": "northwind.example.com",
           "website": "https://northwind.example.com", "email": "ada@northwind.example.com",
           "first_name": "Ada", "last_name": "Lovelace", "firm_domain": "northwind.example.com",
           "company_domain": "northwind.example.com", "firm_website": "https://northwind.example.com"}


class Wire:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        assert self.payloads, "unexpected provider request"
        return 200, self.payloads.pop(0)


@pytest.mark.parametrize("healthy,logged_in,connected", [(True, True, True), (False, True, False),
                                                         (True, False, False), (False, False, False)])
def test_apollo_health_requires_both_flags(healthy, logged_in, connected):
    wire = Wire({"healthy": healthy, "is_logged_in": logged_in})
    result = clients.test_provider_connection("apollo", api_key="fake-key", request_fn=wire)
    assert result["status"] == ("connected" if connected else "response_invalid")
    assert len(wire.calls) == 1


@pytest.mark.parametrize("contact", [
    {"id": 1}, {"id": 1, "firm": "Northwind", "domain": "northwind.example.com"},
    {"id": 1, "partner_name": "Ada Lovelace", "domain": "northwind.example.com"},
])
def test_apollo_incomplete_identity_is_rejected_before_request(contact):
    wire = Wire()
    with pytest.raises(clients.ContactProviderClientError):
        clients.run_apollo_person_enrichment(contact, api_key="fake-key", request_fn=wire)
    assert wire.calls == []


def test_apollo_person_enrichment_keeps_provider_candidate():
    wire = Wire({"person": {"name": "Ada Lovelace", "email": "ada@northwind.example.com",
                             "email_status": "verified", "organization": {"name": "Northwind Labs"}}})
    result = clients.run_apollo_person_enrichment(CONTACT, api_key="fake-key", request_fn=wire)
    assert result.get("email") == CONTACT["email"], result
    assert len(wire.calls) == 1


def test_enrow_find_pending_job_is_bounded_and_does_not_invent_an_email():
    pending = {"id": "find-job-1", "status": "pending"}
    wire = Wire(*[pending] * 3)
    sleeps = []
    result = clients.run_enrow_email_find(CONTACT, api_key="fake-key", request_fn=wire, sleep_fn=sleeps.append)
    assert result["state"] == "timeout", result
    assert result["email"] is None
    assert result["request_count"] == len(wire.calls) == 3
    assert len(sleeps) <= 2


def test_enrow_missing_domain_is_rejected_before_request():
    wire = Wire()
    with pytest.raises(clients.ContactProviderClientError):
        clients.run_enrow_email_find({"id": 1, "partner_name": "Ada Lovelace", "firm": "Northwind Labs"},
                                    api_key="fake-key", request_fn=wire, sleep_fn=lambda _: None)
    assert wire.calls == []


@pytest.mark.parametrize("verdict,expected", [("deliverable", "verified"), ("undeliverable", "invalid")])
def test_enrow_terminal_verification_mapping(verdict, expected):
    wire = Wire({"id": "verify-job-1", "status": "pending"},
                {"id": "verify-job-1", "status": "completed", "result": verdict,
                 "email": CONTACT["email"], "data": {"status": verdict, "result": verdict}})
    result = clients.run_enrow_email_verification(CONTACT, api_key="fake-key", request_fn=wire, sleep_fn=lambda _: None)
    assert result.get("state") == expected, result
    assert len(wire.calls) <= 2
