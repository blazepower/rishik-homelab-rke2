"""Sure-specific chart checks and network-isolated, exact-image Linux acceptance."""
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "apps/sure-finance"
CHART = ROOT / "charts/maybe-finance"
VALUES = yaml.safe_load((APP / "helmrelease.yaml").read_text())["spec"]["values"]
DEFAULTS = yaml.safe_load((CHART / "values.yaml").read_text())
FACTORY = json.loads((APP / "factory-ca.json").read_text())
IMAGE = VALUES["image"]["repository"] + ":" + VALUES["image"]["tag"]
UPGRADE_BASELINE = (
    "ghcr.io/we-promise/sure:0.7.2-alpha.2@sha256:"
    "b5d6d822fdef24c1af06ad455ce003139a2bd50cb6cf76a84a147dfd06b0e3dc"
)
POSTGRES = DEFAULTS["postgresql"]["image"]["repository"] + ":" + DEFAULTS["postgresql"]["image"]["tag"]
REDIS_IMAGE = VALUES.get("redis", {}).get("image", DEFAULTS["redis"]["image"])
REDIS = REDIS_IMAGE["repository"] + ":" + REDIS_IMAGE["tag"]


def render(values):
    result = subprocess.run(
        ["helm", "template", "sure-finance", str(CHART), "--namespace", "sure-finance", "--values", "-"],
        input=yaml.safe_dump(values), text=True, capture_output=True, timeout=60,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def sidekiq_probe_command(container):
    # Factory gems live in BUNDLE_PATH/ruby/3.4.0, not plain GEM_HOME/specifications.
    return ["exec", container, "bundle", "exec", "ruby", "-e", SIDEKIQ_REGISTRATION_PROBE]


def factory_image_binding(image, factory, runtime_id=None, runtime_manifest=None):
    repository, digest = image.split("@")
    repository = repository.rsplit(":", 1)[0]
    if factory["image_index"] != repository + "@" + digest:
        raise AssertionError("selected image index differs from audited factory")
    if runtime_id is None:
        return
    # Classic Docker identifies configs; containerd identifies indexes/manifests.
    amd64 = factory["image_amd64"].split("@")[1]
    if runtime_manifest is not None:
        if runtime_manifest != amd64:
            raise AssertionError("running platform manifest differs from audited factory")
        if runtime_id in (digest, amd64, factory["image_config"]):
            return
    elif runtime_id == factory["image_config"]:
        return
    raise AssertionError("runtime image identity differs from audited factory: " + str(runtime_id))


def probe_diagnostics(result):
    details = {"exit_code": result.returncode}
    for line in (result.stdout + result.stderr).decode(errors="replace").splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if type(data.get("sidekiq_registered")) is bool:
            details["sidekiq_registered"] = data["sidekiq_registered"]
        error = data.get("sidekiq_probe_error")
        if isinstance(error, str) and len(error) <= 80 and re.fullmatch(r"[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*", error):
            details["error_class"] = error
    classes = re.findall(
        r"\((LoadError|SyntaxError|NameError|NoMethodError|ArgumentError|TypeError|Bundler::GemNotFound)\)",
        result.stderr.decode(errors="replace"),
    )
    if classes:
        details["ruby_error_classes"] = sorted(set(classes))
    return details


class SureProbeTests(unittest.TestCase):
    def test_factory_binding_rejects_renovated_index_and_runtime_config_drift(self):
        factory_image_binding(IMAGE, FACTORY, FACTORY["image_config"])
        with self.assertRaisesRegex(AssertionError, "image index"):
            factory_image_binding(IMAGE.rsplit("@", 1)[0] + "@sha256:" + "0" * 64, FACTORY)
        with self.assertRaisesRegex(AssertionError, "image identity"):
            factory_image_binding(IMAGE, FACTORY, "sha256:" + "0" * 64)

    def test_containerd_index_identity_requires_exact_running_amd64_manifest(self):
        index = FACTORY["image_index"].split("@")[1]
        amd64 = FACTORY["image_amd64"].split("@")[1]
        factory_image_binding(IMAGE, FACTORY, index, amd64)
        with self.assertRaisesRegex(AssertionError, "image identity"):
            factory_image_binding(IMAGE, FACTORY, index)
        with self.assertRaisesRegex(AssertionError, "platform manifest"):
            factory_image_binding(IMAGE, FACTORY, index, "sha256:" + "0" * 64)
        with self.assertRaisesRegex(AssertionError, "platform manifest"):
            factory_image_binding(IMAGE, FACTORY, FACTORY["image_config"], "sha256:" + "0" * 64)

    def test_registration_probe_uses_factory_bundle_and_live_sidekiq_api(self):
        command = sidekiq_probe_command("fixture")
        self.assertEqual(command[:6], ["exec", "fixture", "bundle", "exec", "ruby", "-e"])
        self.assertIn('require "sidekiq/api"', command[6])
        self.assertIn("Sidekiq::ProcessSet.new(false).any?", command[6])
        self.assertIn('worker["pid"] == 1', command[6])
        self.assertIn('worker["beat"].to_f > now - 60', command[6])

    def test_failure_retains_probe_exit_class_and_process_state_without_raw_output_or_logs(self):
        case = SureLinuxAcceptance("test_original_rails_and_sidekiq_startup_native_defaults_and_mount_barriers")
        case.containers = ["fixture"]
        result = subprocess.CompletedProcess([], 2, b"private-generated-auth-output\n",
                                            b'{"sidekiq_probe_error":"LoadError","message":"private-error"}\n')
        state = {"Running": True, "ExitCode": 0, "OOMKilled": False, "Error": "private-state"}
        case.docker = mock.Mock(side_effect=[result, subprocess.CompletedProcess([], 0, json.dumps(state).encode())])
        with self.assertRaises(AssertionError) as failure:
            case.wait_ready(sidekiq_probe_command("fixture"), 0, "Sidekiq registration")
        message = str(failure.exception)
        self.assertIn('"exit_code": 2', message)
        self.assertIn('"error_class": "LoadError"', message)
        self.assertIn('"Running": true', message)
        self.assertIn('"OOMKilled": false', message)
        self.assertNotIn("private-", message)
        self.assertEqual([call.args[0] for call in case.docker.call_args_list], ["exec", "inspect"])

    def test_unregistered_probe_is_retried_until_success(self):
        case = SureLinuxAcceptance("test_original_rails_and_sidekiq_startup_native_defaults_and_mount_barriers")
        case.docker = mock.Mock(side_effect=[
            subprocess.CompletedProcess([], 1, b'{"sidekiq_registered":false}\n', b""),
            subprocess.CompletedProcess([], 0, b'{"sidekiq_registered":true}\n', b""),
        ])
        with mock.patch.object(time, "sleep"):
            case.wait_ready(sidekiq_probe_command("fixture"), 1, "Sidekiq registration")
        self.assertEqual(case.docker.call_count, 2)

    def test_unstructured_load_error_retains_class_without_error_message(self):
        result = subprocess.CompletedProcess([], 1, b"", b"private-error (LoadError)\n")
        self.assertEqual(probe_diagnostics(result), {"exit_code": 1, "ruby_error_classes": ["LoadError"]})


class SureChartTests(unittest.TestCase):
    def test_existing_redis_claim_requires_seeded_aof_and_exclusive_rollouts(self):
        values = {**VALUES, "redis": {"enabled": True, "persistence": {"existingClaim": "seeded-redis"}}}
        deployment = next(doc for doc in render(values)
                          if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith("-redis"))
        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(pod["volumes"], [
            {"name": "data", "persistentVolumeClaim": {"claimName": "seeded-redis"}}])
        guard = pod["initContainers"][0]
        self.assertEqual(guard["image"], pod["containers"][0]["image"])
        self.assertEqual(guard["command"], [
            "sh", "-ec", "test -s /data/appendonlydir/appendonly.aof.manifest"])
        self.assertEqual(guard["volumeMounts"], [
            {"name": "data", "mountPath": "/data", "readOnly": True}])
        self.assertEqual(pod["containers"][0]["command"], ["redis-server", "--appendonly", "yes"])
        self.assertEqual(pod["containers"][0]["volumeMounts"], [{"name": "data", "mountPath": "/data"}])
        self.assertEqual(pod["securityContext"]["fsGroup"], 999)

    def test_unset_redis_claim_preserves_existing_ephemeral_pod_template(self):
        values = {**VALUES, "redis": {"enabled": True, "persistence": {"existingClaim": ""}}}
        deployment = next(doc for doc in render(values)
                          if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith("-redis"))
        self.assertNotIn("strategy", deployment["spec"])
        pod = deployment["spec"]["template"]["spec"]
        self.assertNotIn("initContainers", pod)
        self.assertNotIn("volumes", pod)
        self.assertNotIn("command", pod["containers"][0])
        self.assertNotIn("volumeMounts", pod["containers"][0])

    def test_quiesced_migration_keeps_both_job_producers_and_consumers_stopped(self):
        values = {**VALUES, "replicaCount": 0, "sidekiq": {"replicaCount": 0}}
        deployments = [doc for doc in render(values)
                       if doc["kind"] == "Deployment" and not doc["metadata"]["name"].endswith("-redis")]
        self.assertEqual(len(deployments), 2)
        self.assertTrue(all(doc["spec"]["replicas"] == 0 for doc in deployments))
        default = next(doc for doc in render({})
                       if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith("-sidekiq"))
        self.assertEqual(default["spec"]["replicas"], 1)

    def test_policy_is_all_eleven_canonical_spkis_with_crlf_independent_comparison(self):
        canonical = json.loads((ROOT / ".github/security/emudhra-2026.json").read_text())
        keys = (APP / "blocked-spkis.sha256").read_text().splitlines()
        self.assertEqual(len(keys), 11)
        self.assertEqual({key.lower() for key in keys},
                         {item["sha256"].lower() for item in canonical["blocked_spkis"]})
        self.assertEqual(("\r\n".join(keys) + "\r\n").splitlines(), keys)

    def test_same_vendor_version_is_pinned_and_factory_manifest_is_immutable(self):
        self.assertEqual(VALUES["image"]["repository"], DEFAULTS["image"]["repository"])
        self.assertEqual(VALUES["image"]["tag"].split("@")[0], DEFAULTS["image"]["tag"])
        factory_image_binding(IMAGE, FACTORY)
        self.assertRegex(FACTORY["image_amd64"], r"^ghcr.io/we-promise/sure@sha256:[a-f0-9]{64}$")
        self.assertRegex(FACTORY["image_config"], r"^sha256:[a-f0-9]{64}$")

    def test_both_pods_have_credential_free_same_image_initializers_and_readonly_barriers(self):
        pods = [doc["spec"]["template"]["spec"] for doc in render(VALUES)
                if doc["kind"] == "Deployment" and not doc["metadata"]["name"].endswith("-redis")]
        self.assertEqual(len(pods), 2)
        for pod in pods:
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(pod["securityContext"]["fsGroup"], 1000)
            init = pod["initContainers"][0]
            self.assertEqual(init["image"], IMAGE)
            self.assertEqual(init["command"], ["ruby", "/ca-hardening/harden-ca.rb"])
            self.assertNotIn("env", init)
            self.assertNotIn("envFrom", init)
            self.assertEqual({mount["name"] for mount in init["volumeMounts"]},
                             {"sure-ca-tools", "sure-ca-output"})
            app = pod["containers"][0]
            self.assertEqual(app["image"], IMAGE)
            self.assertTrue(app["securityContext"]["readOnlyRootFilesystem"])
            self.assertEqual(app["securityContext"]["capabilities"]["drop"], ["ALL"])
            self.assertEqual(next(item["value"] for item in app["env"] if item["name"] == "TMPDIR"),
                             "/rails/tmp/runtime")
            mounts = {mount["mountPath"]: mount for mount in app["volumeMounts"]}
            for path in ("/etc/ssl/certs", "/usr/share/ca-certificates",
                         "/" + FACTORY["bundles"]["aws"], "/" + FACTORY["bundles"]["skylight"]):
                self.assertTrue(mounts[path]["readOnly"])
            for path in ("/rails/tmp", "/rails/log", "/rails/storage"):
                self.assertIn(path, mounts)
            self.assertNotIn("/etc/ssl/private", mounts)
            self.assertNotIn("/usr/local/share/ca-certificates", mounts)

    def test_owned_tmp_initializer_is_separate_from_trust_and_database_credentials(self):
        pods = [doc["spec"]["template"]["spec"] for doc in render(VALUES)
                if doc["kind"] == "Deployment" and not doc["metadata"]["name"].endswith("-redis")]
        for pod in pods:
            init = pod["initContainers"][1]
            self.assertEqual(init["name"], "prepare-runtime-tmp")
            self.assertEqual(init["image"], IMAGE)
            self.assertNotIn("env", init)
            self.assertNotIn("envFrom", init)
            self.assertEqual(init["volumeMounts"], [
                {"name": "sure-runtime-tmp", "mountPath": "/rails/tmp"}])
            self.assertEqual(init["securityContext"]["runAsUser"], 1000)
            self.assertTrue(init["securityContext"]["readOnlyRootFilesystem"])
            self.assertEqual(init["securityContext"]["capabilities"]["drop"], ["ALL"])

    def test_rails_entrypoint_and_sidekiq_command_are_not_wrapped_or_replaced(self):
        apps = {doc["spec"]["template"]["spec"]["containers"][0]["name"]:
                doc["spec"]["template"]["spec"]["containers"][0]
                for doc in render(VALUES) if doc["kind"] == "Deployment"
                and not doc["metadata"]["name"].endswith("-redis")}
        self.assertNotIn("command", apps["maybe"])
        self.assertNotIn("args", apps["maybe"])
        self.assertEqual(apps["sidekiq"]["command"], ["bundle", "exec", "sidekiq"])
        self.assertNotIn("args", apps["sidekiq"])

    def test_disabled_chart_retains_original_defaults(self):
        for doc in render({}):
            if doc["kind"] != "Deployment":
                continue
            pod = doc["spec"]["template"]["spec"]
            self.assertNotIn("startupProbe", pod["containers"][0])
            self.assertNotIn("initContainers", pod)
            self.assertFalse(any(volume["name"].startswith("sure-ca") for volume in pod.get("volumes", [])))

    def test_cold_start_probe_gates_liveness_without_loosening_steady_state_checks(self):
        deployment = next(doc for doc in render(VALUES)
                          if doc["kind"] == "Deployment"
                          and doc["metadata"]["name"].endswith("maybe-finance"))
        app = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(app["startupProbe"]["httpGet"], DEFAULTS["readinessProbe"]["httpGet"])
        self.assertEqual(app["livenessProbe"], DEFAULTS["livenessProbe"])
        self.assertEqual(app["readinessProbe"], DEFAULTS["readinessProbe"])
        startup_budget = app["startupProbe"]["periodSeconds"] * app["startupProbe"]["failureThreshold"]
        liveness_budget = (app["livenessProbe"]["initialDelaySeconds"]
                           + app["livenessProbe"]["periodSeconds"] * app["livenessProbe"]["failureThreshold"])
        self.assertGreater(startup_budget, liveness_budget)

    def test_existing_optional_rails_storage_is_preserved_and_not_exposed_to_guard(self):
        values = {**VALUES, "persistence": {"appStorage": {"enabled": True}}}
        deployment = next(doc for doc in render(values)
                          if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith("maybe-finance"))
        pod = deployment["spec"]["template"]["spec"]
        volume = next(volume for volume in pod["volumes"] if volume["name"] == "storage")
        self.assertIn("persistentVolumeClaim", volume)
        mount = next(mount for mount in pod["containers"][0]["volumeMounts"]
                     if mount["mountPath"] == "/rails/storage")
        self.assertEqual(mount["name"], "storage")
        self.assertNotIn("storage", {mount["name"] for mount in pod["initContainers"][0]["volumeMounts"]})

    def test_guard_rejects_fs_group_drift_at_render_time(self):
        values = {**VALUES, "podSecurityContext": {"fsGroup": 999}}
        with self.assertRaises(AssertionError):
            render(values)

    def test_policy_configmap_hash_is_referenced_by_helm_values(self):
        directory = ROOT / ("fleet-sure-render-" + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        config = yaml.safe_load((APP / "kustomization.yaml").read_text())
        config["resources"] = ["helmrelease.yaml"]
        for name in ("helmrelease.yaml", "kustomizeconfig.json", "harden-ca.rb",
                     "factory-ca.json", "blocked-spkis.sha256"):
            shutil.copyfile(APP / name, directory / name)
        (directory / "kustomization.yaml").write_text(yaml.safe_dump(config))
        documents = list(yaml.safe_load_all(subprocess.check_output(
            ["kubectl", "kustomize", str(directory)], text=True, timeout=60,
        )))
        config = next(doc for doc in documents if doc["kind"] == "ConfigMap")
        release = next(doc for doc in documents if doc["kind"] == "HelmRelease")
        self.assertTrue(config["metadata"]["name"].startswith("sure-runtime-ca-"))
        self.assertEqual(release["spec"]["values"]["runtimeCaHardening"]["configMapName"],
                         config["metadata"]["name"])
        self.assertEqual(set(config["data"]), {"harden-ca.rb", "factory-ca.json", "blocked-spkis.sha256"})


@unittest.skipUnless(sys.platform.startswith("linux"), "Acceptance runs on the GitHub-hosted Linux Docker runner")
class SureLinuxAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise AssertionError("Linux acceptance requires the approved Docker runner")
        for image in (IMAGE, UPGRADE_BASELINE, POSTGRES, REDIS):
            subprocess.run(["docker", "pull", "--platform=linux/amd64", image],
                           check=True, capture_output=True, timeout=300)
        labels = json.loads(subprocess.run(
            ["docker", "image", "inspect", "--format={{json .Config.Labels}}", UPGRADE_BASELINE],
            check=True, capture_output=True, timeout=30,
        ).stdout)
        if labels.get("org.opencontainers.image.revision") != "53e831039d4757a501dd1dd65d9f9c892d1be1a1":
            raise AssertionError("upgrade fixture differs from the deployed immutable source")

    def setUp(self):
        self.base = ROOT / ("fleet-sure-test-" + uuid.uuid4().hex)
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base)
        self.containers = []
        self.addCleanup(self.cleanup)
        self.tools = self.base / "public-tools"
        self.tools.mkdir()
        for name in ("harden-ca.rb", "factory-ca.json", "blocked-spkis.sha256"):
            shutil.copyfile(APP / name, self.tools / name)

    def cleanup(self):
        for name in reversed(self.containers):
            subprocess.run(["docker", "rm", "--force", "--volumes", name], capture_output=True, timeout=45)

    def docker(self, *args, timeout=90, check=True):
        result = subprocess.run(["docker", *args], capture_output=True, timeout=timeout)
        if check:
            self.assertEqual(result.returncode, 0, "Isolated Docker operation failed: " + " ".join(args[:2]))
        return result

    def create(self, image=IMAGE, options=(), command=(), network="none"):
        name = "sure-ca-test-" + uuid.uuid4().hex[:12]
        self.containers.append(name)
        self.docker("create", "--name", name, "--platform=linux/amd64", "--network=" + network,
                    "--cap-drop=ALL", "--security-opt=no-new-privileges", *options, image, *command)
        if image == IMAGE:
            inspected = json.loads(self.docker("inspect", name).stdout)[0]
            descriptor = inspected.get("ImageManifestDescriptor") or {}
            factory_image_binding(IMAGE, FACTORY, inspected["Image"], descriptor.get("digest"))
        return name

    def initialize(self, tools=None):
        tools = tools or self.tools
        output = self.base / ("output-" + uuid.uuid4().hex[:8])
        output.mkdir(mode=0o777)
        output.chmod(0o777)
        init = self.create(options=[
            "--read-only", "--user=1000:1000", "--entrypoint=/bin/sh",
            "--volume", str(tools) + ":/ca-hardening:ro", "--volume", str(output) + ":/ca-output",
        ], command=["-c", (
            "ruby /ca-hardening/harden-ca.rb; status=$?; "
            # Only disposable public output directories need host-runner cleanup permission.
            "ruby -rfind -e \"Find.find('/ca-output'){|p|File.chmod(0777,p) if File.directory?(p)&&!File.symlink?(p)&&File.stat(p).uid==Process.uid}\"; "
            'exit "$status"'
        )])
        result = self.docker("start", "--attach", init, check=False)
        state = json.loads(self.docker("inspect", "--format={{json .State}}", init).stdout)
        return output, result, state["ExitCode"]

    def app_options(self, output):
        options = ["--read-only", "--user=1000:1000"]
        for source, target in (
            ("os-certs", "/etc/ssl/certs"), ("os-sources", "/usr/share/ca-certificates"),
            ("aws.crt", "/" + FACTORY["bundles"]["aws"]),
            ("skylight.pem", "/" + FACTORY["bundles"]["skylight"]),
        ):
            options += ["--volume", str(output / source) + ":" + target + ":ro"]
        for path in ("/rails/tmp", "/rails/log", "/rails/storage"):
            options += ["--tmpfs", path + ":rw,uid=1000,gid=1000,mode=0775"]
        options += ["--tmpfs=/rails/tmp/runtime:rw,uid=1000,gid=1000,mode=0700"]
        options += ["--volume", str(self.tools) + ":/ca-test-tools:ro"]
        options += ["--volume", str(self.base / "fixtures") + ":/sure-public-fixtures:ro"]
        return options

    def database_fixture(self):
        owner = self.create(options=["--entrypoint=ruby"], command=["-e", "sleep 900"])
        self.docker("start", owner)
        self.assertEqual(self.docker("inspect", "--format={{.HostConfig.NetworkMode}}", owner).stdout.strip(), b"none")
        network = "container:" + owner
        postgres = self.create(POSTGRES, options=[
            "--user=70:70", "--tmpfs=/pgdata:rw,uid=70,gid=70,mode=0700",
            "--tmpfs=/var/run/postgresql:rw,uid=70,gid=70,mode=0775",
            "--env=PGDATA=/pgdata", "--env=POSTGRES_DB=sure_fixture", "--env=POSTGRES_USER=fixture",
            "--env=POSTGRES_PASSWORD=public-test-fixture",  # gitleaks:allow
        ], network=network)
        redis = self.create(REDIS, options=["--read-only", "--user=1000:1000", "--entrypoint=redis-server"],
                            command=["--bind", "127.0.0.1", "--save", "", "--appendonly", "no"], network=network)
        self.docker("start", postgres)
        self.docker("start", redis)
        self.wait_ready(["exec", postgres, "pg_isready", "-U", "fixture", "-d", "sure_fixture"], 60,
                        "fresh disposable PostgreSQL fixture")
        self.wait_ready(["exec", redis, "redis-cli", "ping"], 30, "fresh disposable Redis fixture")
        environment = {
            "RAILS_ENV": "production", "SECRET_KEY_BASE": "1" * 128,
            "SELF_HOSTING_ENABLED": "true", "RAILS_FORCE_SSL": "false", "RAILS_ASSUME_SSL": "false",
            "DB_HOST": "127.0.0.1", "POSTGRES_DB": "sure_fixture", "TMPDIR": "/rails/tmp/runtime",
            "POSTGRES_USER": "fixture", "POSTGRES_PASSWORD": "public-test-fixture",  # gitleaks:allow
            "REDIS_URL": "redis://127.0.0.1:6379/1",
        }
        return network, postgres, environment

    def test_bad_policy_or_factory_drift_prevents_initializer_success_before_app_start(self):
        tools = self.base / "tools"
        shutil.copytree(self.tools, tools)
        original_policy = (tools / "blocked-spkis.sha256").read_text()
        for policy in ("", "bad\n", "\n".join(original_policy.splitlines()[:10]),
                       (original_policy.splitlines()[0] + "\n") * 11):
            (tools / "blocked-spkis.sha256").write_text(policy)
            output, _, code = self.initialize(tools)
            self.assertEqual(code, 78)
            self.assertEqual(list(output.iterdir()), [])
        (tools / "blocked-spkis.sha256").write_bytes(original_policy.replace("\n", "\r\n").encode())
        _, _, code = self.initialize(tools)
        self.assertEqual(code, 0)
        for target in ("system_bundle", "entrypoint"):
            drift = json.loads((self.tools / "factory-ca.json").read_text())
            if target == "system_bundle":
                drift["entries"][drift["bundles"]["system"]]["sha256"] = "0" * 64
            else:
                drift["entrypoint_sha256"] = "0" * 64
            (tools / "factory-ca.json").write_text(json.dumps(drift))
            output, _, code = self.initialize(tools)
            self.assertEqual(code, 78)
            self.assertEqual(list(output.iterdir()), [])

    def test_authenticated_recovery_tool_rejects_wrong_keys_and_corrupt_archives(self):
        name = self.create(options=[
            "--entrypoint=ruby", "--read-only", "--user=1000:1000",
            "--tmpfs=/tmp:rw,uid=1000,gid=1000,mode=0700",
            "--volume", str(ROOT / ".github/scripts") + ":/scripts:ro",
        ], command=["/scripts/sure-encrypted-recovery.rb", "self-test"])
        result = self.docker("start", "--attach", name)
        self.assertEqual(json.loads(result.stdout), {"recovery_crypto_self_test": True})

    def test_new_ruby_uses_owned_tmp_below_foreign_owned_world_writable_parent(self):
        options = ["--entrypoint=ruby", "--read-only", "--user=1000:1000",
                   "--tmpfs=/rails/tmp:rw,uid=0,gid=1000,mode=0777"]
        invalid = self.create(options=[*options, "--env=TMPDIR=/rails/tmp"],
                              command=["-e", "require 'tmpdir';begin;Dir.tmpdir;exit 1;"
                                       "rescue ArgumentError;exit 42;end"])
        self.docker("start", "--attach", invalid, check=False)
        self.assertEqual(json.loads(self.docker("inspect", "--format={{json .State}}", invalid).stdout)["ExitCode"],
                         42)
        deployment = next(doc for doc in render(VALUES) if doc["kind"] == "Deployment"
                          and doc["metadata"]["name"].endswith("maybe-finance"))
        command = deployment["spec"]["template"]["spec"]["initContainers"][1]["command"]
        code = command[2] + (";require 'tmpdir';raise 'wrong temporary directory' unless "
                             "Dir.tmpdir==ENV.fetch('TMPDIR');raise 'unsafe temporary permissions' unless "
                             "File.stat(Dir.tmpdir).mode&0777==0700")
        valid = self.create(options=[*options, "--env=TMPDIR=/rails/tmp/runtime"], command=["-e", code])
        self.docker("start", "--attach", valid)

    def test_original_rails_and_sidekiq_startup_native_defaults_and_mount_barriers(self):
        self.assertEqual(json.loads(self.docker("image", "inspect", "--format={{json .Config.Entrypoint}}",
                                                IMAGE).stdout), ["/rails/bin/docker-entrypoint"])
        self.assertEqual(json.loads(self.docker("image", "inspect", "--format={{json .Config.Cmd}}",
                                                IMAGE).stdout), ["./bin/rails", "server"])
        output, _, code = self.initialize()
        self.assertEqual(code, 0)
        fixtures = self.base / "fixtures"
        fixtures.mkdir()
        baseline = self.create(options=["--entrypoint=ruby", "--volume", str(self.tools) + ":/ca-test-tools:ro"],
                               command=["-e", FIXTURE_PROBE])
        fixture_data = self.docker("start", "--attach", baseline).stdout
        json.loads(fixture_data)
        (fixtures / "public-negative-roots.json").write_bytes(fixture_data)
        network, postgres, environment = self.database_fixture()
        options = self.app_options(output)
        for key, value in environment.items():
            options += ["--env=" + key + "=" + value]
        rails = self.create(options=options, network=network)
        self.docker("start", rails)
        readiness = (
            "require 'net/http';r=Net::HTTP.start('127.0.0.1',3000,open_timeout:1,read_timeout:2)"
            "{|h|h.get('/')};exit((200..399).include?(r.code.to_i) ? 0 : 1)"
        )
        self.wait_ready(["exec", rails, "ruby", "-e", readiness], 180,
                        "original Rails entrypoint/db:prepare with read-only root and three writable runtime directories")
        schema = self.docker("exec", postgres, "psql", "-U", "fixture", "-d", "sure_fixture",
                             "-Atc", "SELECT COUNT(*) FROM schema_migrations").stdout
        self.assertGreater(int(schema.strip()), 0)
        sidekiq = self.create(options=[*options, "--entrypoint=bundle"], command=["exec", "sidekiq"], network=network)
        self.docker("start", sidekiq)
        self.wait_ready(sidekiq_probe_command(sidekiq), 90,
                        "original bundle exec sidekiq process registration in disposable Redis")
        for container in (rails, sidekiq):
            state = json.loads(self.docker("inspect", "--format={{json .State}}", container).stdout)
            self.assertTrue(state["Running"])
            self.docker("exec", container, "ruby", "-e", NATIVE_PROBE)
            self.docker("exec", "--user=0:0", container, "ruby", "-e", READONLY_PROBE)
            self.docker("exec", container, "ruby", "-rsocket", "-e",
                        "abort 'non-loopback interface' unless Socket.getifaddrs.all?"
                        "{|a|!a.addr||!a.addr.ip?||['127.0.0.1','::1'].include?(a.addr.ip_address)}")

    def test_upgrade_from_deployed_schema_restores_before_original_candidate_migrations(self):
        (self.base / "fixtures").mkdir()
        output, _, code = self.initialize()
        self.assertEqual(code, 0)
        network, postgres, environment = self.database_fixture()
        options = self.app_options(output)
        options += ["--env=" + key + "=" + value for key, value in environment.items()]
        readiness = (
            "require 'net/http';r=Net::HTTP.start('127.0.0.1',3000,open_timeout:1,read_timeout:2)"
            "{|h|h.get('/')};exit((200..399).include?(r.code.to_i) ? 0 : 1)"
        )
        old = self.create(UPGRADE_BASELINE, options=options, network=network)
        self.docker("start", old)
        self.wait_ready(["exec", old, "ruby", "-e", readiness], 180, "actual deployed-image schema fixture")
        query = ["exec", postgres, "psql", "-U", "fixture", "-d", "sure_fixture", "-At", "-v", "ON_ERROR_STOP=1"]
        self.assertEqual(self.docker(*query, "-c", "SELECT max(version) FROM schema_migrations").stdout.strip(),
                         b"20260531153000")
        self.assertEqual(self.docker(*query, "-c",
                         "SELECT to_regclass('public.trade_republic_items') IS NULL").stdout.strip(), b"t")
        self.docker(*query, "-c",
                    "INSERT INTO families(id,name,currency,created_at,updated_at) VALUES "
                    "('00000000-0000-0000-0000-000000000001','Public upgrade fixture','USD',now(),now())")
        self.docker("stop", "--time=30", old)
        self.docker("exec", postgres, "pg_dump", "-U", "fixture", "-d", "sure_fixture",
                    "--format=custom", "--file=/pgdata/upgrade-fixture.dump")
        self.docker("exec", postgres, "createdb", "-U", "fixture", "sure_upgrade_restore")
        self.docker("exec", postgres, "pg_restore", "-U", "fixture", "-d", "sure_upgrade_restore",
                    "--exit-on-error", "/pgdata/upgrade-fixture.dump")
        self.docker("exec", postgres, "rm", "/pgdata/upgrade-fixture.dump")
        restored = [part.replace("POSTGRES_DB=sure_fixture", "POSTGRES_DB=sure_upgrade_restore")
                    for part in options]
        candidate = self.create(options=restored, network=network)
        self.docker("start", candidate)
        self.wait_ready(["exec", candidate, "ruby", "-e", readiness], 360,
                        "restored deployed-schema upgrade through unchanged candidate entrypoint")
        restored_query = [part.replace("sure_fixture", "sure_upgrade_restore") for part in query]
        self.assertEqual(self.docker(*restored_query, "-c",
                         "SELECT name||':'||currency FROM families WHERE "
                         "id='00000000-0000-0000-0000-000000000001'").stdout.strip(),
                         b"Public upgrade fixture:USD")
        self.docker("exec", candidate, "./bin/rails", "runner",
                    "abort 'pending migration' if ActiveRecord::Base.connection_pool.migration_context.needs_migration?",
                    timeout=60)
        self.assertEqual(self.docker(*restored_query, "-c",
                         "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' "
                         "AND table_name='trade_republic_items' AND column_name='session_expires_at'").stdout.strip(),
                         b"1")
        self.docker("exec", candidate, "ruby", "-e", READONLY_PROBE)

    def wait_ready(self, command, seconds, description):
        deadline = time.monotonic() + seconds
        while True:
            last_probe = self.docker(*command, timeout=10, check=False)
            if last_probe.returncode == 0:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
        container = command[1]
        self.assertIn(container, self.containers)
        inspected = self.docker("inspect", "--format={{json .State}}", container, check=False)
        try:
            state = json.loads(inspected.stdout) if inspected.returncode == 0 else {}
        except ValueError:
            state = {}
        public_state = {key: state[key] for key in ("Running", "ExitCode", "OOMKilled") if key in state}
        self.fail(description + " acceptance failed (disposable fixture only): "
                  + json.dumps({"last_probe": probe_diagnostics(last_probe), "container_state": public_state}))


SIDEKIQ_REGISTRATION_PROBE = r"""
require "json"
begin
 require "sidekiq/api"
 Sidekiq.logger.level = Logger::FATAL
 Sidekiq.configure_client{|config|config.redis={url:ENV.fetch("REDIS_URL")}}
 now=Time.now.to_f
 registered=Sidekiq::ProcessSet.new(false).any? do |worker|
  worker["pid"] == 1 && worker["beat"].to_f > now - 60
 end
 puts JSON.generate({sidekiq_registered:registered})
 exit(registered ? 0 : 1)
rescue LoadError, StandardError => error
 warn JSON.generate({sidekiq_probe_error:error.class.name})
 exit 2
end
"""


NATIVE_PROBE = r"""
require "openssl"; require "json"
require "tmpdir"
raise "unsafe application temporary directory" unless Dir.tmpdir=="/rails/tmp/runtime" &&
 File.writable?(Dir.tmpdir) && File.stat(Dir.tmpdir).uid==Process.uid && File.stat(Dir.tmpdir).mode&0777==0700
load "/ca-test-tools/harden-ca.rb"
m=JSON.parse(File.read("/ca-test-tools/factory-ca.json"))
denied=SureTrust.policy(File.read("/ca-test-tools/blocked-spkis.sha256"))
base="/sure-public-fixtures/public-negative-roots.json"
fixtures=JSON.parse(File.read(base))
bad=fixtures.fetch("denied").map{|pem|OpenSSL::X509::Certificate.new(pem)}
good=OpenSSL::X509::Certificate.new(fixtures.fetch("positive"))
raise "entrypoint changed" unless SureTrust.sha(File.binread("/rails/bin/docker-entrypoint"))==m.fetch("entrypoint_sha256")
fixtures.fetch("preserved").each do |path,identities|
 actual=SureTrust.certificates(File.binread("/"+path)).map{|row|row[2]}.sort
 raise "unaffected root loss" unless actual==identities.sort
end
fixtures.fetch("removed").each{|path|raise "blocked source remains" if File.exist?("/"+path)}
store=OpenSSL::X509::Store.new; store.set_default_paths
raise "native default failed" unless store.verify(good) && bad.none?{|c|store.verify(c)}
m.fetch("bundles").each do |name,path|
 bytes=File.binread("/"+path); certs=SureTrust.certificates(bytes)
 raise "denied vendor/system root" if certs.any?{|row|denied.include?(row[1])}
 before=m.fetch("counts").fetch(name).fetch("after")
 raise "unexpected root count" unless certs.length==before
 s=OpenSSL::X509::Store.new;s.add_file("/"+path)
 raise "native vendor control failed" unless s.verify(good) && bad.none?{|c|s.verify(c)}
 begin;File.open("/"+path,"wb"){};raise "writable bundle";rescue Errno::EROFS,Errno::EACCES;end
end
for directory in ["/etc/ssl/certs","/usr/share/ca-certificates",
                  "/usr/local/bundle/ruby","/usr/local/bundle/ruby/3.4.0/gems"]
 begin;File.rename(directory,directory+"-forbidden");raise "replaceable trust ancestor";rescue Errno::EROFS,Errno::EACCES,Errno::EBUSY;end
end
custom=OpenSSL::X509::Store.new;custom.add_cert(bad.first)
raise "custom context semantics changed" unless custom.verify(bad.first)
puts "native default/vendor rejection, positive controls and replacement barriers verified"
"""

READONLY_PROBE = r"""
require "json"
m=JSON.parse(File.read("/ca-test-tools/factory-ca.json"))
m.fetch("bundles").each_value do |path|
 begin
  File.open("/"+path,"wb"){}
  raise "writable public bundle"
 rescue Errno::EROFS
 end
end
for directory in ["/etc/ssl/certs","/usr/share/ca-certificates",
                  "/usr/local/bundle/ruby","/usr/local/bundle/ruby/3.4.0/gems"]
 begin
  File.rename(directory,directory+"-forbidden")
  raise "replaceable public trust ancestor"
 rescue Errno::EROFS,Errno::EBUSY
 end
end
puts "root public trust write/rename attempts failed with filesystem barriers"
"""

FIXTURE_PROBE = r"""
load "/ca-test-tools/harden-ca.rb"
m=JSON.parse(File.read("/ca-test-tools/factory-ca.json"))
denied=SureTrust.policy(File.read("/ca-test-tools/blocked-spkis.sha256"))
bundles=m.fetch("bundles").values.map{|path|SureTrust.certificates(File.binread("/"+path))}
common=bundles.map{|rows|rows.map{|row|row[2]}.to_set}.reduce(:&)
positive=bundles.first.find{|row|common.include?(row[2])&&!denied.include?(row[1])&&row[3].issuer==row[3].subject&&row[3].not_before<=Time.now&&row[3].not_after>=Time.now}
raise "no unaffected common native control" unless positive
bad=bundles.flatten(1).select{|row|denied.include?(row[1])}.uniq{|row|row[1]}
default=OpenSSL::X509::Store.new;default.set_default_paths
raise "factory baseline not reproduced" unless default.verify(positive[3])&&bad.all?{|row|default.verify(row[3])}
preserved={};removed=[]
m.fetch("entries").each do |path,record|
 next unless record.fetch("kind")=="file"
 rows=SureTrust.certificates(File.binread("/"+path)).reject{|row|denied.include?(row[1])}
 rows.empty? ? removed.push(path) : preserved[path]=rows.map{|row|row[2]}
end
puts JSON.generate({denied:bad.map{|row|row[0]},positive:positive[0],preserved:preserved,removed:removed})
"""


if __name__ == "__main__":
    unittest.main()
