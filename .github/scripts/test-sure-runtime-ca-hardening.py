"""Sure-specific chart checks and network-isolated, exact-image Linux acceptance."""
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "apps/sure-finance"
CHART = ROOT / "charts/maybe-finance"
VALUES = yaml.safe_load((APP / "helmrelease.yaml").read_text())["spec"]["values"]
DEFAULTS = yaml.safe_load((CHART / "values.yaml").read_text())
FACTORY = json.loads((APP / "factory-ca.json").read_text())
IMAGE = VALUES["image"]["repository"] + ":" + VALUES["image"]["tag"]
POSTGRES = DEFAULTS["postgresql"]["image"]["repository"] + ":" + DEFAULTS["postgresql"]["image"]["tag"]
REDIS = DEFAULTS["redis"]["image"]["repository"] + ":" + DEFAULTS["redis"]["image"]["tag"]


def render(values):
    result = subprocess.run(
        ["helm", "template", "sure-finance", str(CHART), "--namespace", "sure-finance", "--values", "-"],
        input=yaml.safe_dump(values), text=True, capture_output=True, timeout=60,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


class SureChartTests(unittest.TestCase):
    def test_policy_is_all_eleven_canonical_spkis_with_crlf_independent_comparison(self):
        canonical = json.loads((ROOT / ".github/security/emudhra-2026.json").read_text())
        keys = (APP / "blocked-spkis.sha256").read_text().splitlines()
        self.assertEqual(len(keys), 11)
        self.assertEqual({key.lower() for key in keys},
                         {item["sha256"].lower() for item in canonical["blocked_spkis"]})
        self.assertEqual(("\r\n".join(keys) + "\r\n").splitlines(), keys)

    def test_same_vendor_version_is_pinned_and_factory_manifest_is_immutable(self):
        self.assertEqual(VALUES["image"]["tag"],
                         "0.7.2-alpha.2@sha256:b5d6d822fdef24c1af06ad455ce003139a2bd50cb6cf76a84a147dfd06b0e3dc")
        self.assertEqual(FACTORY["image_amd64"],
                         "ghcr.io/we-promise/sure@sha256:0b58098fc7f5c189f348abcc64be1858d7789f8f724b8465d0a2a2c48a4e4b55")
        self.assertEqual(len(FACTORY["entries"]), 453)
        self.assertEqual(FACTORY["counts"]["system"]["after"], 148)
        self.assertEqual(FACTORY["counts"]["aws"]["after"], 134)
        self.assertEqual(FACTORY["counts"]["skylight"]["after"], 126)

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
                             "/rails/tmp")
            mounts = {mount["mountPath"]: mount for mount in app["volumeMounts"]}
            for path in ("/etc/ssl/certs", "/usr/share/ca-certificates",
                         "/" + FACTORY["bundles"]["aws"], "/" + FACTORY["bundles"]["skylight"]):
                self.assertTrue(mounts[path]["readOnly"])
            for path in ("/rails/tmp", "/rails/log", "/rails/storage"):
                self.assertIn(path, mounts)
            self.assertNotIn("/etc/ssl/private", mounts)
            self.assertNotIn("/usr/local/share/ca-certificates", mounts)

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
            self.assertNotIn("initContainers", pod)
            self.assertFalse(any(volume["name"].startswith("sure-ca") for volume in pod.get("volumes", [])))

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
        for image in (IMAGE, POSTGRES, REDIS):
            subprocess.run(["docker", "pull", "--platform=linux/amd64", image],
                           check=True, capture_output=True, timeout=300)

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
        options += ["--volume", str(self.tools) + ":/ca-test-tools:ro"]
        options += ["--volume", str(self.base / "fixtures") + ":/sure-public-fixtures:ro"]
        return options

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
        drift = json.loads((tools / "factory-ca.json").read_text())
        drift["entries"][drift["bundles"]["system"]]["sha256"] = "0" * 64
        (tools / "factory-ca.json").write_text(json.dumps(drift))
        output, _, code = self.initialize(tools)
        self.assertEqual(code, 78)
        self.assertEqual(list(output.iterdir()), [])

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
        owner = self.create(options=["--entrypoint=ruby"], command=["-e", "sleep 900"])
        self.docker("start", owner)
        self.assertEqual(self.docker("inspect", "--format={{.HostConfig.NetworkMode}}", owner).stdout.strip(), b"none")
        network = "container:" + owner
        postgres = self.create(POSTGRES, options=[
            "--user=70:70", "--tmpfs=/pgdata:rw,uid=70,gid=70,mode=0700",
            "--tmpfs=/var/run/postgresql:rw,uid=70,gid=70,mode=0775",
            "--env=PGDATA=/pgdata", "--env=POSTGRES_DB=sure_fixture", "--env=POSTGRES_USER=fixture",
            "--env=POSTGRES_PASSWORD=public-test-fixture",
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
            "DB_HOST": "127.0.0.1", "POSTGRES_DB": "sure_fixture",
            "TMPDIR": "/rails/tmp",
            "POSTGRES_USER": "fixture", "POSTGRES_PASSWORD": "public-test-fixture",
            "REDIS_URL": "redis://127.0.0.1:6379/1",
        }
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
        self.wait_ready([
            "exec", sidekiq, "ruby", "-rredis", "-e",
            "exit(Redis.new(url:ENV.fetch('REDIS_URL')).scard('processes')>0 ? 0 : 1)",
        ], 90, "original bundle exec sidekiq process registration in disposable Redis")
        for container in (rails, sidekiq):
            state = json.loads(self.docker("inspect", "--format={{json .State}}", container).stdout)
            self.assertTrue(state["Running"])
            self.docker("exec", container, "ruby", "-e", NATIVE_PROBE)
            self.docker("exec", "--user=0:0", container, "ruby", "-e", READONLY_PROBE)
            self.docker("exec", container, "ruby", "-rsocket", "-e",
                        "abort 'non-loopback interface' unless Socket.getifaddrs.all?"
                        "{|a|!a.addr||!a.addr.ip?||['127.0.0.1','::1'].include?(a.addr.ip_address)}")

    def wait_ready(self, command, seconds, description):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.docker(*command, timeout=10, check=False).returncode == 0:
                return
            time.sleep(1)
        container = command[1]
        self.assertIn(container, self.containers)
        logs = self.docker("logs", "--tail=40", container, check=False)
        text = (logs.stdout + logs.stderr).decode(errors="replace")
        public_lines = [line for line in text.splitlines()
                        if not re.search(r"secret|token|password|credential|https?://", line, re.I)]
        self.fail(description + " acceptance failed (disposable fixture only):\n"
                  + "\n".join(public_lines)[-2400:])


NATIVE_PROBE = r"""
require "openssl"; require "json"
require "tmpdir"
raise "unwritable application temporary directory" unless Dir.tmpdir=="/rails/tmp" && File.writable?(Dir.tmpdir)
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
