"""
cert_utils.fetch_cert_info tests.

This is the only plugin code that performs live network I/O. Here the socket and
TLS layers are fully mocked: we feed a locally-generated self-signed certificate's
DER bytes through the mocked socket and assert the *parsing* result. No connection
is ever made.
"""

import datetime
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.test import SimpleTestCase

from ..cert_utils import fetch_cert_info


def build_self_signed_der(cn='dhcp.example.com', sans=('dhcp.example.com',)):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc))
        .not_valid_after(datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return cert, cert.public_bytes(serialization.Encoding.DER)


class FetchCertInfoTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cert, cls.der = build_self_signed_der()

    def _call(self):
        ssock = mock.MagicMock()
        ssock.getpeercert.return_value = self.der
        wrap_cm = mock.MagicMock()
        wrap_cm.__enter__.return_value = ssock
        with mock.patch('netbox_windows_dhcp.cert_utils.socket.create_connection'), \
                mock.patch('ssl.SSLContext.wrap_socket', return_value=wrap_cm):
            return fetch_cert_info('dhcp.example.com', 443)

    def test_parses_subject_and_issuer(self):
        info = self._call()
        self.assertEqual(info['subject_cn'], 'dhcp.example.com')
        self.assertEqual(info['issuer_cn'], 'dhcp.example.com')

    def test_parses_sans(self):
        self.assertEqual(self._call()['sans'], ['dhcp.example.com'])

    def test_not_after_is_timezone_aware(self):
        not_after = self._call()['not_after']
        self.assertIsNotNone(not_after.tzinfo)

    def test_pem_round_trips(self):
        self.assertTrue(self._call()['pem'].startswith('-----BEGIN CERTIFICATE-----'))

    def test_fingerprint_matches(self):
        expected = self.cert.fingerprint(hashes.SHA256()).hex().upper()
        expected = ':'.join(expected[i:i + 2] for i in range(0, 64, 2))
        self.assertEqual(self._call()['fingerprint'], expected)

    def test_empty_cert_raises(self):
        ssock = mock.MagicMock()
        ssock.getpeercert.return_value = None
        wrap_cm = mock.MagicMock()
        wrap_cm.__enter__.return_value = ssock
        with mock.patch('netbox_windows_dhcp.cert_utils.socket.create_connection'), \
                mock.patch('ssl.SSLContext.wrap_socket', return_value=wrap_cm):
            with self.assertRaises(ValueError):
                fetch_cert_info('dhcp.example.com', 443)
