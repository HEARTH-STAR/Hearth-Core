import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from routers.hearth_agent import watch as watch_route
from server import app
from services.hearth_agent.connectors.github.repo import RepoMirror
from services.hearth_agent.watch import discover, impact
from services.hearth_agent.watch.scheduler import WatchScheduler
from services.hearth_agent.watch.store import get_store


def _names(found):
    return {f.provider.id: sorted(f.packages) for f in found}


class MatchTests(unittest.TestCase):
    def test_providers_from_every_manifest_kind(self):
        found = discover.match({
            "package.json": json.dumps({"dependencies": {"stripe": "^17", "react": "19"},
                                        "devDependencies": {"@octokit/rest": "21"}}),
            "api/requirements.txt": "openai>=1.0  # the model\n-r base.txt\nrequests\n",
            "worker/pyproject.toml": '[project]\nname = "worker"\ndependencies = ["Slack_SDK[optional]>=3.2", "httpx"]\n',
            "bot/Pipfile": "[packages]\ntwilio = \"*\"\n",
            "Poetry/pyproject.toml": "[tool.poetry.dependencies]\npython = \"^3.11\"\nPyGithub = \"^2\"\n",
        })
        self.assertEqual(_names(found), {
            "stripe": ["stripe"], "openai": ["openai"], "github": ["@octokit/rest", "pygithub"],
            "twilio": ["twilio"], "slack": ["slack-sdk"],
        })
        self.assertEqual(discover.match({"package.json": '{"dependencies": {"react": "19"}}'}), [])

    def test_validation_commands_follow_the_repo(self):
        pkg = {"scripts": {"build": "tsc", "test": "vitest run", "lint": "eslint ."}}
        manifests = {"package.json": json.dumps({**pkg, "dependencies": {"stripe": "1"}})}
        found = discover.match(manifests)
        self.assertEqual(discover.validation_commands(manifests, {"package.json", "package-lock.json"}, found),
                         ["npm ci", "npm run build", "npm run lint", "npm test"])
        self.assertEqual(discover.validation_commands(manifests, {"package.json", "pnpm-lock.yaml"}, found)[0],
                         "pnpm install --frozen-lockfile")

        placeholder = {"web/package.json": json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'},
                                                       "dependencies": {"openai": "4"}})}
        self.assertEqual(
            discover.validation_commands(placeholder, {"web/package.json", "web/tsconfig.json"},
                                         discover.match(placeholder)),
            ["cd web && npm install", "cd web && npx tsc --noEmit"])

        py = {"requirements.txt": "stripe\n"}
        commands = discover.validation_commands(py, {"requirements.txt", "tests/test_pay.py"}, discover.match(py))
        self.assertEqual(commands[0], "python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt")
        self.assertIn("pytest", commands[-1])


class SdkImpactTests(unittest.TestCase):
    CHANGES = [{"type": "param_removed", "symbol": "POST /v1/payment_intents", "kind": "operation",
                "detail": "payment_method_types", "breaking": True}]

    def scan(self, files, packages=("stripe",)):
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in files.items():
                (Path(tmp) / name).write_text(text, encoding="utf-8")
            return [(m.path, m.line, m.symbol) for m in impact.scan(Path(tmp), self.CHANGES, packages)]

    def test_sdk_calls_match_in_files_that_import_the_sdk(self):
        got = self.scan({
            "pay.ts": "import Stripe from 'stripe';\n"
                      "const pi = await stripe.paymentIntents.create({\n"
                      "  payment_method_types: ['card'],\n});\n",
            "pay.py": "import stripe\nstripe.PaymentIntent.create()\nclient.payment_intents.create(payment_method_types=['card'])\n",
            # no SDK import: the same names are someone else's
            "other.ts": "thing.paymentIntents.create({ payment_method_types: [] })\n",
        })
        self.assertEqual(got, [
            ("pay.py", 2, "POST /v1/payment_intents"),  # stripe-python: stripe.PaymentIntent.create
            ("pay.py", 3, "POST /v1/payment_intents"),
            ("pay.py", 3, "payment_method_types"),
            ("pay.ts", 2, "POST /v1/payment_intents"),
            ("pay.ts", 3, "payment_method_types"),
        ])

    def test_without_known_packages_names_are_searched_everywhere(self):
        got = self.scan({"other.ts": "x({ payment_method_types: [] })\n"}, packages=())
        self.assertEqual(got, [("other.ts", 1, "payment_method_types")])


def _git(*args, cwd):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class RepoRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"HEARTH_STATE_DIR": str(root / "state"), "HEARTH_API_WATCH": "0"})
        self.env.start()
        work, remote = root / "work", root / "remote.git"
        work.mkdir()
        (work / "package.json").write_text(json.dumps({
            "scripts": {"test": "vitest run"}, "dependencies": {"stripe": "^17", "openai": "^4"}}), encoding="utf-8")
        (work / "package-lock.json").write_text("{}", encoding="utf-8")
        (work / "node_modules").mkdir()
        (work / "node_modules" / "package.json").write_text('{"dependencies": {"twilio": "1"}}', encoding="utf-8")
        _git("init", "-q", "-b", "main", cwd=work)
        _git("add", "-A", "-f", cwd=work)
        _git("commit", "-q", "-m", "init", cwd=work)
        _git("clone", "-q", "--bare", str(work), str(remote), cwd=root)
        url = mock.patch.object(RepoMirror, "url", new_callable=mock.PropertyMock, return_value=str(remote))
        url.start()
        self.addCleanup(url.stop)
        self.scheduler = WatchScheduler()
        sched = mock.patch.object(watch_route, "get_scheduler", return_value=self.scheduler)
        sched.start()
        self.addCleanup(sched.stop)
        self.client = TestClient(app)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_watches_the_apis_the_repo_uses(self):
        with mock.patch.object(self.scheduler, "check_soon") as soon:
            r = self.client.post("/api/watch/repo", json={"repo": "acme/shop"})
        self.assertEqual(r.status_code, 201, r.text)
        out = r.json()
        self.assertEqual((out["branch"], out["validate_commands"]), ("main", ["npm ci", "npm test"]))
        self.assertEqual([p["id"] for p in out["providers"]], ["stripe", "openai"])  # node_modules is skipped
        self.assertEqual([(w["name"], w["packages"], w["auto_pr"], w["repo"]) for w in out["watches"]],
                         [("Stripe", ["stripe"], True, "acme/shop"), ("OpenAI", ["openai"], True, "acme/shop")])
        self.assertEqual(soon.call_count, 2)

        again = self.client.post("/api/watch/repo", json={"repo": "acme/shop"}).json()
        self.assertEqual((again["watches"], len(again["existing"])), ([], 2))  # no duplicate watches
        self.assertEqual(len(get_store().watches), 2)

    def test_errors(self):
        self.assertEqual(self.client.post("/api/watch/repo", json={"repo": "nope"}).status_code, 422)
        with mock.patch.object(discover, "match", return_value=[]):
            r = self.client.post("/api/watch/repo", json={"repo": "acme/shop"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("uses none of the APIs HEARTH knows", r.json()["detail"])
        with mock.patch.object(RepoMirror, "url", new_callable=mock.PropertyMock,
                               return_value=str(Path(self.tmp.name) / "missing.git")):
            r = self.client.post("/api/watch/repo", json={"repo": "acme/gone"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Could not read acme/gone", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
