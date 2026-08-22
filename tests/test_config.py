import base64
import copy
import importlib.machinery
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).parents[1]
CONFIG = ROOT / "userpatches/overlay/usr/local/sbin/adsb-config"
spec = importlib.util.spec_from_loader("adsb_config", importlib.machinery.SourceFileLoader("adsb_config", str(CONFIG)))
adsb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adsb)

VALID = {
    "schemaVersion": 2,
    "setupComplete": True,
    "receiver": {"name": "test-receiver", "latitude": 41.0, "longitude": -87.0, "altitudeMeters": 200, "gain": "auto", "device": "auto"},
    "network": {"ethernet": {"enabled": True, "interface": "end0"}, "wifi": {"configured": False, "interface": "wlan0"}},
    "listeners": {
        "beast": {"enabled": True, "address": "0.0.0.0", "port": 30005},
        "jsonHttp": {"enabled": True, "address": "0.0.0.0", "port": 8080},
    },
    "outboundConnectors": [],
    "admin": {"enabled": True, "address": "127.0.0.1", "port": 8443, "tls": True},
    "configurationMode": {"apGateway": "192.168.77.1", "apPrefixLength": 24, "recoveryMarker": "/boot/adsb-receiver/recovery-request"},
    "advanced": {"ppm": 0, "maxRangeNmi": 300},
}


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temporary.name)
        self.originals = (adsb.ETC, adsb.STATE, adsb.RUN, adsb.BOOT, adsb.NM_CONNECTIONS)
        adsb.ETC = root / "etc"
        adsb.STATE = root / "state"
        adsb.RUN = root / "run"
        adsb.BOOT = root / "boot"
        adsb.NM_CONNECTIONS = root / "nm"
        for directory in (adsb.ETC, adsb.STATE, adsb.RUN, adsb.BOOT, adsb.NM_CONNECTIONS):
            directory.mkdir()

    def tearDown(self):
        adsb.ETC, adsb.STATE, adsb.RUN, adsb.BOOT, adsb.NM_CONNECTIONS = self.originals
        self.temporary.cleanup()

    def write_config(self, config):
        path = adsb.RUN / "candidate.json"
        path.write_text(json.dumps(config))
        return path

    def test_schema_rejects_unknown_fields_public_admin_and_port_collision(self):
        bad = copy.deepcopy(VALID)
        bad["receiver"]["extraArgs"] = ["--write-json=/etc"]
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            adsb.validate(bad)
        bad = copy.deepcopy(VALID)
        bad["admin"]["address"] = "0.0.0.0"
        with self.assertRaisesRegex(ValueError, "allowed bind"):
            adsb.validate(bad)
        bad = copy.deepcopy(VALID)
        bad["admin"]["port"] = 8080
        with self.assertRaisesRegex(ValueError, "distinct ports"):
            adsb.validate(bad)

    def test_generated_readsb_arguments_use_upstream_listener_flags(self):
        rendered = adsb.render_args(VALID)
        self.assertIn("--net-bind-address\n0.0.0.0\n--net-bo-port\n30005\n", rendered)
        self.assertIn("--write-json\n/run/readsb\n--write-json-every\n1\n", rendered)
        self.assertNotIn("--alt", rendered)
        self.assertNotIn("--net-api-port", rendered)
        connector = copy.deepcopy(VALID)
        connector["outboundConnectors"] = [{"enabled": True, "host": "feed.internal", "port": 30004, "protocol": "beast_reduce_plus_out"}]
        self.assertIn("feed.internal,30004,beast_reduce_plus_out", adsb.render_args(connector))

    def test_factory_first_boot_decodes_without_network_or_location(self):
        factory = json.loads((ROOT / "userpatches/overlay/usr/share/adsb-receiver/default-config.json").read_text())
        self.assertTrue(adsb.configuration_mode(adsb.validate(factory)))
        rendered = adsb.render_args(factory)
        self.assertIn("--device-type\nrtlsdr", rendered)
        self.assertNotIn("--lat", rendered)
        self.assertNotIn("config", rendered.lower())

    def test_recovery_marker_requires_exact_regular_file(self):
        marker = adsb.BOOT / "recovery-request"
        marker.write_text("anything\n")
        self.assertFalse(adsb.marker_valid(marker))
        marker.write_bytes(adsb.RECOVERY_TEXT)
        self.assertTrue(adsb.marker_valid(marker))
        marker.unlink()
        target = adsb.BOOT / "target"
        target.write_bytes(adsb.RECOVERY_TEXT)
        marker.symlink_to(target)
        self.assertFalse(adsb.marker_valid(marker))

    def test_run_mode_does_not_follow_link_state(self):
        self.assertFalse(adsb.configuration_mode(VALID, adsb.BOOT / "absent"))

    def test_runtime_mode_file_preserves_explicit_recovery_state(self):
        (adsb.ETC / "config.json").write_bytes(adsb.canonical(VALID))
        (adsb.RUN / "mode").write_text("configuration\n")
        with patch.object(adsb.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertEqual("configuration", adsb.status_document()["mode"])

    def test_atomic_apply_and_last_known_good_rollback_on_failed_restart(self):
        first = self.write_config(VALID)
        self.assertTrue(adsb.apply(first, initial=True))
        original = (adsb.ETC / "config.json").read_bytes()
        (adsb.ETC / "admin-password.json").write_text(json.dumps(adsb.hash_password("correct horse battery staple")))
        changed = copy.deepcopy(VALID)
        changed["receiver"]["name"] = "changed"
        candidate = self.write_config(changed)

        def inactive(*arguments, **kwargs):
            return subprocess.CompletedProcess(arguments, 1 if "is-active" in arguments else 0, "", "")

        with patch.object(adsb, "run_systemctl", side_effect=inactive):
            with self.assertRaisesRegex(RuntimeError, "did not become active"):
                adsb.apply(candidate)
        self.assertEqual(original, (adsb.ETC / "config.json").read_bytes())
        self.assertEqual(original, (adsb.STATE / "last-known-good.json").read_bytes())
        self.assertTrue((adsb.STATE / "last-apply-error.json").exists())

    def test_explicit_rollback_restores_last_known_good(self):
        first = self.write_config(VALID)
        adsb.apply(first, initial=True)
        (adsb.ETC / "admin-password.json").write_text(json.dumps(adsb.hash_password("correct horse battery staple")))
        changed = copy.deepcopy(VALID)
        changed["receiver"]["name"] = "manually-corrupted"
        (adsb.ETC / "config.json").write_bytes(adsb.canonical(changed))
        (adsb.STATE / "readsb.args").write_text(adsb.render_args(changed))
        with patch.object(adsb, "run_systemctl", return_value=subprocess.CompletedProcess([], 0, "", "")):
            adsb.rollback()
        self.assertEqual("test-receiver", json.loads((adsb.ETC / "config.json").read_text())["receiver"]["name"])

    def test_secret_redaction_is_recursive(self):
        source = {"wifiPassword": "one", "nested": [{"admin_secret": "two"}], "receiver": "safe"}
        self.assertEqual(adsb.redact(source), {"wifiPassword": "[redacted]", "nested": [{"admin_secret": "[redacted]"}], "receiver": "safe"})

    def test_admin_basic_auth_and_csrf(self):
        credential = adsb.hash_password("correct horse battery staple")
        header = "Basic " + base64.b64encode(b"admin:correct horse battery staple").decode()
        self.assertTrue(adsb.basic_authorized(header, credential))
        self.assertFalse(adsb.basic_authorized("Basic " + base64.b64encode(b"admin:wrong password").decode(), credential))
        self.assertTrue(adsb.csrf_valid("token", "token", "token", "https://192.168.1.2:8443", "192.168.1.2:8443"))
        self.assertFalse(adsb.csrf_valid("token", None, "token", "https://192.168.1.2:8443", "192.168.1.2:8443"))
        self.assertFalse(adsb.csrf_valid("token", "token", "token", "https://evil.example", "192.168.1.2:8443"))

    def test_wifi_profile_keeps_secret_out_of_command_model(self):
        profile = adsb.wifi_keyfile("Home WiFi", "not-a-real-secret", "wlan0").decode()
        self.assertIn("psk=not-a-real-secret", profile)
        self.assertIn("route-metric=600", profile)
        with self.assertRaises(ValueError):
            adsb.wifi_keyfile("bad\nssid", "not-a-real-secret", "wlan0")

    def test_firewall_limits_wildcard_listeners_to_private_sources(self):
        rules = adsb.firewall_rules(VALID)
        self.assertIn("tcp dport { 8080, 30005 }", rules)
        self.assertIn("10.0.0.0/8", rules)
        self.assertIn("fc00::/7", rules)


if __name__ == "__main__":
    unittest.main()
