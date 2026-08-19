import importlib.machinery, importlib.util, json, pathlib, tempfile, unittest
from unittest.mock import patch

AGENT = pathlib.Path(__file__).parents[1] / "userpatches/overlay/usr/local/sbin/adsb-config-agent"
spec = importlib.util.spec_from_loader("agent", importlib.machinery.SourceFileLoader("agent", str(AGENT)))
agent = importlib.util.module_from_spec(spec); spec.loader.exec_module(agent)

VALID = {"schemaVersion": 1, "receiver": {"id": "r1", "latitude": 1.0, "longitude": 2.0, "altitudeMeters": 3, "gain": "auto"}, "readsb": {"extraArgs": ["--fix"]}, "outputs": [{"host": "adsb.example", "port": 30004, "protocol": "beast_reduce_plus_out", "enabled": True}], "configRefreshSeconds": 900}

class AgentTests(unittest.TestCase):
    def test_valid_config_renders_safe_args(self):
        self.assertIn("--net-connector\nadsb.example,30004,beast_reduce_plus_out", agent.render_args(agent.validate(VALID)))
    def test_rejects_invalid_schema_and_dangerous_argument(self):
        bad = json.loads(json.dumps(VALID)); bad["schemaVersion"] = 2
        with self.assertRaises(ValueError): agent.validate(bad)
        bad = json.loads(json.dumps(VALID)); bad["readsb"]["extraArgs"] = ["--write-json=/etc"]
        with self.assertRaises(ValueError): agent.validate(bad)
    def test_rejects_bad_output_and_interval(self):
        bad = json.loads(json.dumps(VALID)); bad["outputs"][0]["host"] = "bad host"
        with self.assertRaises(ValueError): agent.validate(bad)
        bad = json.loads(json.dumps(VALID)); bad["configRefreshSeconds"] = 1
        with self.assertRaises(ValueError): agent.validate(bad)
    def test_atomic_apply_and_same_config_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory); agent.STATE = root / "state"; agent.ETC = root / "etc"; agent.SYSTEMD = root / "systemd"; agent.STATE.mkdir(); agent.ETC.mkdir()
            source = root / "config.json"; source.write_text(json.dumps(VALID))
            with patch.object(agent.subprocess, "run"):
                self.assertTrue(agent.apply(source))
                self.assertFalse(agent.apply(source))
            self.assertTrue((agent.STATE / "active.json").exists())

if __name__ == "__main__": unittest.main()
