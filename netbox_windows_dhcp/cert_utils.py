"""
Utilities for fetching and parsing TLS certificates from PSU servers.

Uses the stdlib ssl module for fetching and the cryptography library (available
in NetBox's venv) for parsing.  A single CERT_NONE connection fetches the DER
bytes; the cryptography library parses them without a second TLS handshake.
This works for both self-signed and CA-signed certificates.
"""

import socket
import ssl
from datetime import timezone


def fetch_cert_info(hostname: str, port: int) -> dict:
    """
    Fetch and parse the TLS certificate from hostname:port without verifying it.

    Returns a dict with keys:
        pem           — PEM string
        subject_cn    — Common Name from the subject
        sans          — list of DNS SAN values
        issuer_cn     — Common Name from the issuer
        not_after     — expiry as timezone-aware datetime (UTC)
        fingerprint   — SHA-256 fingerprint (colon-separated hex, uppercase)

    Raises ssl.SSLError, OSError, or ValueError on failure.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with socket.create_connection((hostname, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
            der = ssock.getpeercert(binary_form=True)

    if not der:
        raise ValueError(f'No certificate returned by {hostname}:{port}')

    pem = ssl.DER_cert_to_PEM_cert(der)

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import ExtensionOID, NameOID

    cert = x509.load_der_x509_certificate(der)

    subject_cn = next(
        (attr.value for attr in cert.subject if attr.oid == NameOID.COMMON_NAME), ''
    )
    issuer_cn = next(
        (attr.value for attr in cert.issuer if attr.oid == NameOID.COMMON_NAME), ''
    )

    try:
        san_ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        sans = list(san_ext.value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        sans = []

    # not_valid_after_utc is timezone-aware (cryptography 42+);
    # not_valid_after is a naive UTC datetime in older versions.
    try:
        not_after = cert.not_valid_after_utc
    except AttributeError:
        not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)

    fp_hex = cert.fingerprint(hashes.SHA256()).hex().upper()
    fingerprint = ':'.join(fp_hex[i:i + 2] for i in range(0, 64, 2))

    return {
        'pem': pem,
        'subject_cn': subject_cn,
        'sans': sans,
        'issuer_cn': issuer_cn,
        'not_after': not_after,
        'fingerprint': fingerprint,
    }
