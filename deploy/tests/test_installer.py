import ast
import base64
import importlib.util
import json
from pathlib import Path
import unittest

DEPLOY = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


installer = module('installer', DEPLOY / 'installer_template.py')
packager = module('packager', DEPLOY / 'package_installer.py')


class EnvMergeTests(unittest.TestCase):
    def test_preserves_existing_keys_and_comments(self):
        original = (
            "# cost-api config\n"
            "DATABASE_URL=postgresql://x\n"
            "COST_INGEST_SECRET=abc\n"
            "TELEPHONY_URL=https://voice.example\n"
        )
        result = installer.update_env(original, {
            "ELEVENLABS_AGENT_URL": "https://cakeworld.example/elevenlabs-agent",
            "ELEVENLABS_AGENT_API_KEY": "new-key",
            "ELEVENLABS_POLL_INTERVAL_SECONDS": "60",
        })
        self.assertIn("# cost-api config\n", result)
        self.assertIn("DATABASE_URL=postgresql://x\n", result)
        self.assertIn("COST_INGEST_SECRET=abc\n", result)
        self.assertIn("TELEPHONY_URL=https://voice.example\n", result)
        values = installer.env_values(result)
        self.assertEqual(values["ELEVENLABS_AGENT_URL"], "https://cakeworld.example/elevenlabs-agent")
        self.assertEqual(values["ELEVENLABS_AGENT_API_KEY"], "new-key")
        self.assertEqual(values["ELEVENLABS_POLL_INTERVAL_SECONDS"], "60")

    def test_replaces_existing_key_in_place_not_appended_twice(self):
        original = "ELEVENLABS_AGENT_API_KEY=old\nOTHER=keep\n"
        result = installer.update_env(original, {"ELEVENLABS_AGENT_API_KEY": "new"})
        self.assertEqual(result.count("ELEVENLABS_AGENT_API_KEY"), 1)
        self.assertIn("ELEVENLABS_AGENT_API_KEY=new\n", result)
        self.assertIn("OTHER=keep\n", result)

    def test_rejects_value_with_newline_injection(self):
        with self.assertRaises(RuntimeError):
            installer.update_env("", {"KEY": "value\nINJECTED=yes"})

    def test_handles_export_prefixed_lines(self):
        original = "export TELEPHONY_URL=https://old\n"
        result = installer.update_env(original, {"TELEPHONY_URL": "https://new"})
        self.assertIn("export TELEPHONY_URL=https://new\n", result)


class PackagerTests(unittest.TestCase):
    def test_bundle_embeds_exact_current_source(self):
        output = packager.build()
        text = output.read_text()
        parsed = ast.parse(text)
        payload_literal = next(
            n.value for n in ast.walk(parsed)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "PAYLOAD" for t in n.targets)
        )
        encoded = ast.literal_eval(payload_literal)
        payload = json.loads(base64.b64decode(encoded))
        self.assertEqual(set(payload), set(packager.FILES))
        for name in packager.FILES:
            self.assertEqual(payload[name], (packager.REPO_ROOT / name).read_text())


if __name__ == "__main__":
    unittest.main()
