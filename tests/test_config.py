import base64
import copy
import importlib.machinery
import importlib.util
import json
import ipaddress
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

DHCP = {"method": "dhcp", "address": None, "prefixLength": None, "gateway": None, "dns": []}
VALID = {
    "schemaVersion": 3,
    "setupComplete": True,
    "receiver": {"name": "test-receiver", "latitude": 41.0, "longitude": -87.0, "altitudeMeters": 200, "gain": "auto", "device": "auto"},
    "network": {"ethernet": {"enabled": True, "interface": "end0", "ipv4": DHCP}, "wifi": {"configured": False, "interface": "wlan0", "ipv4": DHCP}},
    "listeners": {"beast": {"enabled": True, "address": "0.0.0.0", "port": 30005}, "jsonHttp": {"enabled": True, "address": "0.0.0.0", "port": 8080}},
    "admin": {"enabled": True, "address": "127.0.0.1", "port": 8443, "tls": True},
    "configurationMode": {"apGateway": "192.168.77.1", "apPrefixLength": 24, "recoveryMarker": "/boot/adsb-bootstrap/recovery-request", "factoryResetMarker": "/boot/adsb-bootstrap/factory-reset"},
    "advanced": {"ppm": 0, "maxRangeNmi": 300},
}


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temporary.name)
        self.originals = (adsb.ETC, adsb.STATE, adsb.RUN, adsb.BOOT, adsb.NM_CONNECTIONS, adsb.DEFAULT_CONFIG)
        adsb.ETC, adsb.STATE, adsb.RUN, adsb.BOOT, adsb.NM_CONNECTIONS = (root / name for name in ("etc", "state", "run", "boot", "nm"))
        adsb.DEFAULT_CONFIG = root / "factory.json"
        for directory in (adsb.ETC, adsb.STATE, adsb.RUN, adsb.BOOT, adsb.NM_CONNECTIONS):
            directory.mkdir()
        factory = copy.deepcopy(VALID)
        factory["setupComplete"] = False
        for field in ("latitude", "longitude", "altitudeMeters"):
            factory["receiver"][field] = None
        adsb.DEFAULT_CONFIG.write_bytes(adsb.canonical(factory))

    def tearDown(self):
        adsb.ETC, adsb.STATE, adsb.RUN, adsb.BOOT, adsb.NM_CONNECTIONS, adsb.DEFAULT_CONFIG = self.originals
        self.temporary.cleanup()

    def candidate(self, config=VALID):
        path = adsb.RUN / "candidate.json"
        path.write_bytes(adsb.canonical(config))
        return path

    def test_schema_v3_rejects_outbound_connectors_and_unknown_fields(self):
        bad = copy.deepcopy(VALID)
        bad["outboundConnectors"] = []
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            adsb.validate(bad)
        bad = copy.deepcopy(VALID)
        bad["schemaVersion"] = 2
        with self.assertRaisesRegex(ValueError, "schemaVersion must be 3"):
            adsb.validate(bad)

    def test_static_ipv4_validation_and_rendering(self):
        static = copy.deepcopy(VALID)
        static["network"]["ethernet"]["ipv4"] = {"method": "static", "address": "192.168.50.4", "prefixLength": 24, "gateway": "192.168.50.1", "dns": ["192.168.50.1"]}
        adsb.validate(static)
        profile = adsb.ethernet_keyfile("end0", static["network"]["ethernet"]["ipv4"]).decode()
        self.assertIn("method=manual", profile)
        self.assertIn("address1=192.168.50.4/24,192.168.50.1", profile)
        self.assertIn("dns=192.168.50.1;", profile)

    def test_static_ipv4_rejects_loopback_multicast_and_invalid_prefix(self):
        for address, prefix in (("127.0.0.2", 24), ("224.0.0.2", 24), ("192.168.50.4", 33)):
            bad = copy.deepcopy(VALID)
            bad["network"]["ethernet"]["ipv4"] = {"method": "static", "address": address, "prefixLength": prefix, "gateway": "192.168.51.1", "dns": ["8.8.8.8"]}
            with self.assertRaises(ValueError):
                adsb.validate(bad)

    def test_setup_wired_address_policy_is_exactly_rfc1918(self):
        for address in ("10.4.3.2", "172.31.9.8", "192.168.12.7"):
            self.assertTrue(adsb.is_rfc1918(ipaddress.ip_address(address)))
        for address in ("8.8.8.8", "169.254.1.2", "127.0.0.1", "203.0.113.4"):
            self.assertFalse(adsb.is_rfc1918(ipaddress.ip_address(address)))

    def test_generated_readsb_arguments_have_no_outbound_connector(self):
        rendered = adsb.render_args(VALID)
        self.assertIn("--net-bo-port\n30005\n", rendered)
        self.assertNotIn("--net-connector", rendered)

    def test_exact_markers_latch_before_consumption_and_recovery_persists(self):
        recovery = adsb.BOOT / "recovery-request"
        recovery.write_bytes(adsb.RECOVERY_TEXT)
        self.assertEqual("recovery", adsb.latch_boot_requests())
        self.assertFalse(recovery.exists())
        self.assertTrue((adsb.STATE / adsb.RECOVERY_LATCH).exists())
        self.assertTrue(adsb.configuration_mode(VALID))
        self.assertEqual("recovery", adsb.latch_boot_requests())

    def test_factory_reset_has_precedence_and_is_exact(self):
        (adsb.BOOT / "recovery-request").write_bytes(adsb.RECOVERY_TEXT)
        (adsb.BOOT / "factory-reset").write_bytes(adsb.FACTORY_RESET_TEXT)
        self.assertEqual("factory-reset", adsb.latch_boot_requests())
        self.assertTrue((adsb.BOOT / "recovery-request").exists())
        wrong = adsb.BOOT / "wrong"
        wrong.write_bytes(adsb.FACTORY_RESET_TEXT + b"extra")
        self.assertFalse(adsb.marker_valid(wrong, adsb.FACTORY_RESET_TEXT))

    def test_factory_reset_removes_mutable_state_but_preserves_release_inputs(self):
        (adsb.STATE / adsb.FACTORY_RESET_LATCH).write_text("pending\n")
        (adsb.ETC / "admin-password.json").write_text("secret")
        (adsb.ETC / "repository-commit").write_text("abc")
        (adsb.NM_CONNECTIONS / "adsb-wifi.nmconnection").write_text("psk=secret")
        adsb.factory_reset()
        self.assertFalse((adsb.ETC / "admin-password.json").exists())
        self.assertFalse((adsb.NM_CONNECTIONS / "adsb-wifi.nmconnection").exists())
        self.assertEqual("abc", (adsb.ETC / "repository-commit").read_text())
        self.assertFalse(json.loads((adsb.ETC / "config.json").read_text())["setupComplete"])

    def test_apply_commits_credential_only_after_health(self):
        adsb.apply(self.candidate(), initial=True, staged_credential=adsb.hash_password("original credential value"))
        old_credential = (adsb.ETC / "admin-password.json").read_bytes()
        old_args = (adsb.STATE / "readsb.args").read_bytes()
        old_firewall = (adsb.STATE / "firewall.nft").read_bytes()
        old_lkg = (adsb.STATE / "last-known-good.json").read_bytes()
        changed = copy.deepcopy(VALID)
        changed["receiver"]["name"] = "changed"
        calls = 0
        def health(_config):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("new runtime unhealthy")
        with patch.object(adsb, "run_systemctl", return_value=subprocess.CompletedProcess([], 0, "", "")):
            with self.assertRaisesRegex(RuntimeError, "unhealthy"):
                adsb.apply(self.candidate(changed), staged_credential=adsb.hash_password("replacement credential"), health=health)
        self.assertEqual(old_credential, (adsb.ETC / "admin-password.json").read_bytes())
        self.assertEqual(old_args, (adsb.STATE / "readsb.args").read_bytes())
        self.assertEqual(old_firewall, (adsb.STATE / "firewall.nft").read_bytes())
        self.assertEqual(old_lkg, (adsb.STATE / "last-known-good.json").read_bytes())
        self.assertEqual("test-receiver", json.loads((adsb.ETC / "config.json").read_text())["receiver"]["name"])
        self.assertEqual("succeeded", json.loads((adsb.STATE / "last-apply-error.json").read_text())["rollback"])

    def test_successful_apply_commits_staged_credential(self):
        credential = adsb.hash_password("new administrator credential")
        with patch.object(adsb, "run_systemctl", return_value=subprocess.CompletedProcess([], 0, "", "")):
            adsb.apply(self.candidate(), staged_credential=credential, health=lambda _config: None)
        self.assertTrue(adsb.password_matches("new administrator credential", json.loads((adsb.ETC / "admin-password.json").read_text())))

    def test_rollback_failure_is_distinct_and_forces_recovery(self):
        adsb.apply(self.candidate(), initial=True, staged_credential=adsb.hash_password("original credential value"))
        changed = copy.deepcopy(VALID)
        changed["receiver"]["name"] = "changed"
        with patch.object(adsb, "run_systemctl", side_effect=[subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0), RuntimeError("rollback restart failed")]):
            with self.assertRaisesRegex(RuntimeError, "rollback failed"):
                adsb.apply(self.candidate(changed), health=lambda _config: (_ for _ in ()).throw(RuntimeError("apply health failed")))
        self.assertTrue((adsb.STATE / adsb.RECOVERY_LATCH).exists())
        self.assertIn("failed", json.loads((adsb.STATE / "last-apply-error.json").read_text())["rollback"])

    def test_bounded_health_requires_continuous_stability(self):
        ticks = iter([0, 0, 1, 2, 3, 4, 5, 6])
        calls = []
        adsb.wait_for_health(VALID, timeout=8, stable_for=3, probe=lambda _config: calls.append(1) or ([] if len(calls) != 3 else ["transient"]), now=lambda: next(ticks), sleeper=lambda _seconds: None)
        self.assertGreaterEqual(len(calls), 6)

    def test_bounded_health_times_out_deterministically(self):
        ticks = iter([0, 0, 1, 2, 3])
        with self.assertRaisesRegex(RuntimeError, "timed out after 3s"):
            adsb.wait_for_health(VALID, timeout=3, stable_for=2, probe=lambda _config: ["JSON 503"], now=lambda: next(ticks), sleeper=lambda _seconds: None)

    def test_json_initial_503_can_recover_before_deadline(self):
        ticks = iter([0, 0, 1, 2, 3, 4, 5, 6])
        attempts = []
        def probe(_config):
            attempts.append(1)
            return ["JSON 503"] if len(attempts) <= 2 else []
        adsb.wait_for_health(VALID, timeout=8, stable_for=3, probe=probe, now=lambda: next(ticks), sleeper=lambda _seconds: None)
        self.assertGreaterEqual(len(attempts), 6)

    def test_health_probe_reports_unavailable_beast_socket(self):
        config = copy.deepcopy(VALID)
        config["listeners"]["jsonHttp"]["enabled"] = False
        with patch.object(adsb, "run_systemctl", return_value=subprocess.CompletedProcess([], 0)), patch.object(adsb.socket, "create_connection", side_effect=ConnectionRefusedError("refused")), patch.object(adsb.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            errors = adsb.health_probe(config)
        self.assertTrue(any("Beast listener unavailable" in error for error in errors))

    def test_firewall_includes_setup_port_independent_of_run_admin(self):
        config = copy.deepcopy(VALID)
        config["admin"]["enabled"] = False
        rules = adsb.firewall_rules(config, setup_mode=True)
        self.assertIn("8443", rules)
        self.assertIn("10.0.0.0/8", rules)

    def test_wifi_candidate_and_final_share_static_ipv4_but_unique_ids(self):
        profile = {"method": "static", "address": "10.10.10.4", "prefixLength": 24, "gateway": "10.10.10.1", "dns": ["10.10.10.1"]}
        candidate = adsb.wifi_keyfile("Home WiFi", "not-a-real-secret", "wlan0", profile, "adsb-wifi-candidate").decode()
        final = adsb.wifi_keyfile("Home WiFi", "not-a-real-secret", "wlan0", profile).decode()
        self.assertIn("id=adsb-wifi-candidate", candidate)
        self.assertIn("address1=10.10.10.4/24,10.10.10.1", candidate)
        self.assertIn("id=adsb-wifi", final)

    def test_existing_wifi_ipv4_rewrite_preserves_psk(self):
        original = adsb.wifi_keyfile("Home WiFi", "not-a-real-secret", "wlan0", DHCP)
        static = {"method": "static", "address": "10.10.10.4", "prefixLength": 24, "gateway": "10.10.10.1", "dns": []}
        updated = adsb.replace_ipv4_section(original, static, 600).decode()
        self.assertIn("psk=not-a-real-secret", updated)
        self.assertIn("method=manual", updated)
        ipv4_section = updated.split("[ipv4]\n", 1)[1].split("[ipv6]", 1)[0]
        self.assertNotIn("method=auto", ipv4_section)

    def test_wifi_candidate_failure_always_cleans_up_and_restores_previous(self):
        final = adsb.NM_CONNECTIONS / "adsb-wifi.nmconnection"
        previous = adsb.wifi_keyfile("Old WiFi", "old-secret-value", "wlan0", DHCP)
        final.write_bytes(previous)
        calls = []
        def fake_nmcli(*arguments, **_kwargs):
            calls.append(arguments)
            if arguments[:4] == ("-g", "GENERAL.CONNECTION", "device", "show"):
                return subprocess.CompletedProcess(arguments, 0, "old-profile\n", "")
            if arguments[:3] == ("connection", "up", "adsb-wifi-candidate"):
                raise subprocess.CalledProcessError(10, arguments)
            return subprocess.CompletedProcess(arguments, 0, "", "")
        with patch.object(adsb, "nmcli", side_effect=fake_nmcli):
            with self.assertRaises(subprocess.CalledProcessError):
                adsb.configure_wifi("New WiFi", "new-secret-value", "wlan0", DHCP)
        self.assertEqual(previous, final.read_bytes())
        self.assertFalse((adsb.RUN / "adsb-wifi-candidate.nmconnection").exists())
        self.assertIn(("connection", "delete", "adsb-wifi-candidate"), calls)
        self.assertIn(("connection", "up", "old-profile"), calls)

    def test_wifi_final_failure_cleans_candidate_and_restores_previous(self):
        final = adsb.NM_CONNECTIONS / "adsb-wifi.nmconnection"
        previous = adsb.wifi_keyfile("Old WiFi", "old-secret-value", "wlan0", DHCP)
        final.write_bytes(previous)
        calls = []
        def fake_nmcli(*arguments, **_kwargs):
            calls.append(arguments)
            if arguments[:4] == ("-g", "GENERAL.CONNECTION", "device", "show"):
                return subprocess.CompletedProcess(arguments, 0, "old-profile\n", "")
            if arguments[:3] == ("connection", "up", "adsb-wifi"):
                raise subprocess.CalledProcessError(11, arguments)
            return subprocess.CompletedProcess(arguments, 0, "", "")
        with patch.object(adsb, "nmcli", side_effect=fake_nmcli), patch.object(adsb, "interface_has_ipv4", return_value=True):
            with self.assertRaises(subprocess.CalledProcessError):
                adsb.configure_wifi("New WiFi", "new-secret-value", "wlan0", DHCP)
        self.assertEqual(previous, final.read_bytes())
        self.assertFalse((adsb.RUN / "adsb-wifi-candidate.nmconnection").exists())
        self.assertIn(("connection", "delete", "adsb-wifi-candidate"), calls)

    def test_network_orchestration_restores_wifi_if_ethernet_fails(self):
        wifi_transaction = {"saved": {}, "interface": "wlan0", "previous": "old", "connection": "adsb-wifi"}
        with patch.object(adsb, "configure_wifi", return_value=wifi_transaction), patch.object(adsb, "configure_ethernet", side_effect=RuntimeError("Ethernet activation failed")), patch.object(adsb, "restore_network") as restore:
            with self.assertRaisesRegex(RuntimeError, "Ethernet activation failed"):
                adsb.configure_network(VALID, {"ssid": "Home", "password": "not-a-real-secret"}, False)
        restore.assert_called_once_with(wifi_transaction)

    def test_diagnostics_redacts_config_and_profile_secrets(self):
        adsb.apply(self.candidate(), initial=True, staged_credential=adsb.hash_password("original credential value"))
        (adsb.NM_CONNECTIONS / "adsb-wifi.nmconnection").write_text("[wifi-security]\npsk=super-secret\n[connection]\nid=adsb-wifi\n")
        output = pathlib.Path(self.temporary.name) / "diagnostics.tar.gz"
        with patch.object(adsb, "status_document", return_value={"mode": "run"}), patch.object(adsb, "command", return_value="safe"):
            adsb.diagnostics(output)
        import tarfile
        with tarfile.open(output) as archive:
            content = b"\n".join(item.read() for item in (archive.extractfile(member) for member in archive.getmembers()) if item).decode()
        self.assertNotIn("super-secret", content)
        self.assertNotIn("original credential value", content)
        self.assertIn("id=adsb-wifi", content)

    def test_recursive_secret_redaction_includes_hashes_and_credentials(self):
        source = {"wifiPassword": "one", "credential": {"hash": "two"}, "receiver": "safe"}
        self.assertEqual({"wifiPassword": "[redacted]", "credential": "[redacted]", "receiver": "safe"}, adsb.redact(source))

    def test_admin_auth_and_csrf(self):
        credential = adsb.hash_password("correct horse battery staple")
        header = "Basic " + base64.b64encode(b"admin:correct horse battery staple").decode()
        self.assertTrue(adsb.basic_authorized(header, credential))
        self.assertTrue(adsb.csrf_valid("token", "token", "token", "https://192.168.1.2:8443", "192.168.1.2:8443"))

    def test_complete_configuration_clears_latch_and_temporary_credentials(self):
        for path in (adsb.STATE / adsb.RECOVERY_LATCH, adsb.STATE / "bootstrap-state.json", adsb.BOOT / "setup-credentials.txt"):
            path.write_text("temporary")
        adsb.complete_configuration_mode()
        self.assertFalse(any(path.exists() for path in (adsb.STATE / adsb.RECOVERY_LATCH, adsb.STATE / "bootstrap-state.json", adsb.BOOT / "setup-credentials.txt")))


if __name__ == "__main__":
    unittest.main()
