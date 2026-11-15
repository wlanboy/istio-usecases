from datetime import datetime, timezone

import pytest

from conftest import fixture_items, fixture_text

EC_NOT_AFTER = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)   # UTCTime, DER-Laenge 0x82 0x017f
RSA_NOT_AFTER = datetime(2055, 1, 1, tzinfo=timezone.utc)         # GeneralizedTime


def der(tag: int, content: bytes) -> bytes:
    """Minimaler DER-Encoder, nur Kurzform der Laenge (< 128 Byte)."""
    assert len(content) < 0x80
    return bytes([tag, len(content)]) + content


def v1_cert(not_after: bytes, time_tag: int) -> bytes:
    """X.509 v1 ohne Versionsfeld [0]; Signatur und Key sind fuer den Parser irrelevant."""
    validity = der(0x30, der(0x17, b"250101000000Z") + der(time_tag, not_after))
    tbs = der(0x30, der(0x02, b"\x01") + der(0x30, b"") + der(0x30, b"") + validity + der(0x30, b""))
    return der(0x30, tbs + der(0x30, b"") + der(0x03, b"\x00"))


@pytest.fixture
def frozen_now(hc, monkeypatch):
    """Setzt datetime.now() im Healthcheck auf einen festen Zeitpunkt."""
    def freeze(moment: datetime) -> None:
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment
        monkeypatch.setattr(hc, "datetime", FrozenDatetime)
    return freeze


# --- DER-Parser ---------------------------------------------------------

def test_der_read_short_and_long_length(hc):
    assert hc.der_read(b"\x30\x03abc", 0) == (0x30, 2, 5)
    assert hc.der_read(b"\x04\x81\x80" + b"x" * 0x80, 0) == (0x04, 3, 3 + 0x80)
    assert hc.der_read(b"\x00\x30\x82\x01\x7f", 1) == (0x30, 5, 5 + 0x17F)


def test_cert_not_after_utctime(hc):
    [cert] = hc.pem_certs(fixture_text("ec-utctime.pem"))
    assert hc.cert_not_after(cert) == EC_NOT_AFTER


def test_cert_not_after_generalizedtime(hc):
    [cert] = hc.pem_certs(fixture_text("rsa-generalized.pem"))
    assert hc.cert_not_after(cert) == RSA_NOT_AFTER


def test_cert_not_after_without_version_field(hc):
    assert hc.cert_not_after(v1_cert(b"491231235959Z", 0x17)) == datetime(2049, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert hc.cert_not_after(v1_cert(b"20610101000000Z", 0x18)) == datetime(2061, 1, 1, tzinfo=timezone.utc)


def test_pem_certs_chain(hc):
    certs = hc.pem_certs(fixture_text("chain.pem"))
    assert [hc.cert_not_after(c) for c in certs] == [EC_NOT_AFTER, RSA_NOT_AFTER]
    assert hc.pem_certs("kein Zertifikat") == []


# --- Bewertung der Laufzeit ---------------------------------------------

def test_cert_finding_uses_earliest_cert_of_chain(hc, frozen_now):
    frozen_now(datetime(2030, 1, 1, tzinfo=timezone.utc))
    status, text = hc.cert_finding("Kette", fixture_text("chain.pem"), warn_days=30)
    assert status == hc.OK
    assert "gueltig bis 2030-06-01" in text


def test_cert_finding_warns_before_expiry(hc, frozen_now):
    frozen_now(datetime(2030, 5, 20, tzinfo=timezone.utc))
    status, text = hc.cert_finding("EC", fixture_text("ec-utctime.pem"), warn_days=30)
    assert status == hc.WARN
    assert "noch 12 Tage" in text


def test_cert_finding_fails_when_expired(hc, frozen_now):
    frozen_now(datetime(2030, 6, 2, tzinfo=timezone.utc))
    status, text = hc.cert_finding("EC", fixture_text("ec-utctime.pem"), warn_days=30)
    assert status == hc.FAIL
    assert "abgelaufen seit 2030-06-01" in text


def test_cert_finding_unreadable(hc):
    assert hc.cert_finding("leer", "", 30) == (hc.WARN, "leer: kein PEM-Zertifikat gefunden")
    broken = "-----BEGIN CERTIFICATE-----\nMAM=\n-----END CERTIFICATE-----\n"  # nur 30 03
    status, text = hc.cert_finding("kaputt", broken, 30)
    assert status == hc.WARN and "nicht lesbar" in text


# --- credentialName -> Namespace der Gateway-Pods -----------------------

def test_gateway_credentials(hc):
    targets = hc.gateway_credentials(fixture_items("gateways.json"), fixture_items("pods.json"))
    assert targets == [
        ("app-default/internal-gw", "gw-one", "internal-cert"),
        ("app-default/internal-gw", "gw-two", "internal-cert"),
        ("istio-system/public-gw", "istio-ingress", "public-cert"),
        ("istio-system/public-gw", "shared", "shared-cert"),
    ]


# --- Revisionen der Namespaces ------------------------------------------

def test_mesh_namespaces(hc):
    assert hc.mesh_namespaces(fixture_items("namespaces.json")) == {
        "app-default": "default",
        "app-canary": "canary",
        "app-both": "default",   # istio-injection hat Vorrang vor istio.io/rev
    }


def test_check_dataplane_reports_pod_without_sidecar_and_unknown_revision(hc):
    namespaces = (fixture_items("namespaces.json"), None)
    pods = (fixture_items("pods.json"), None)
    findings = hc.check_dataplane(namespaces, pods, ([], None), {"default"}, restart_warn=5)
    assert (hc.WARN, "Namespace app-default: 1 Pods ohne Sidecar "
                     "(vor Aktivierung der Injection gestartet?): web-2") in findings
    assert any(s == hc.FAIL and "app-canary: istio.io/rev=canary" in t for s, t in findings)


# --- STALE-Erkennung ----------------------------------------------------

def test_check_sync_reports_stale(hc):
    entries = [
        {"proxy": "web-1.app-default", "istiod": "istiod-a",
         "cluster_sent": "n1", "cluster_acked": "n1", "listener_sent": "n2", "listener_acked": "n1"},
        {"proxy": "web-3.app-default", "istiod": "istiod-a", "cluster_sent": "n1", "cluster_acked": "n1"},
    ]
    findings = hc.check_sync((entries, []), ([], None), "istio-system")
    assert (hc.WARN, "1 Proxys STALE: web-1.app-default (LDS)") in findings
