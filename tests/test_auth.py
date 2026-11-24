import sys
from pathlib import Path

import ldap3
from flask import Flask
from ldap3.utils.ntlm import NtlmClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'micro_status_flask'))
from auth import domain_auth


def test_ntlm_password_hash_is_available():
    # A local NTLM calculation exercises the MD4 provider without contacting LDAP.
    assert len(NtlmClient('example', 'test', 'not-a-real-password').ntowf_v2()) == 16


def test_ldap_failure_logs_reason_without_password(monkeypatch, caplog):
    password = 'example-private-password'

    def fail_connection(server, user, password, authentication):
        raise ValueError(f'Bind failed with password {password}')

    monkeypatch.setattr(ldap3, 'Connection', fail_connection)
    with Flask(__name__).app_context():
        assert domain_auth('test', password, domain_server='ldap://localhost:389') is None

    assert 'LDAP authentication failed (ValueError): Bind failed with password [redacted]' in caplog.text
    assert password not in caplog.text
