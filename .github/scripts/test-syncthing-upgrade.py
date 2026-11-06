"""Disposable official-image migration rehearsal; never mounts production data."""

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
import uuid
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
OLD_IMAGE = "syncthing/syncthing@sha256:35401e343a2a20372fac11c576d8f8fe2de02b4f26128e805b4715258d6205a5"
NEW_IMAGE = "syncthing/syncthing@sha256:c28e86f5173bacc429aef92af915b5c4fac56e1a9dcd3875855fd51f664d0414"
API_KEY = "disposable-syncthing-migration-fixture"
FOLDER = "synthetic-books"
BASE_ARGS = ["serve", "--home=/config", "--gui-address=0.0.0.0:8384"]
OLD_ARGS = [*BASE_ARGS, "--no-default-folder"]
NEW_ARGS = [*BASE_ARGS, "--db-delete-retention-interval=0"]


def command(*args, input=None, timeout=180):
    result = subprocess.run(
        list(args), input=input, capture_output=True, timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} exited {result.returncode}")
    return result.stdout


def fixture_config(content, other_id, other_name):
    root = ET.fromstring(content)
    own_id = root.find("device").get("id")
    for folder in list(root.findall("folder")):
        root.remove(folder)
    folder = ET.SubElement(root, "folder", {
        "id": FOLDER, "label": "Synthetic books", "path": "/data/books",
        "type": "sendreceive", "rescanIntervalS": "1",
    })
    ET.SubElement(folder, "fsWatcherEnabled").text = "false"
    ET.SubElement(folder, "ignorePerms").text = "true"
    for device_id in (own_id, other_id):
        ET.SubElement(folder, "device", {"id": device_id})
    peer = ET.SubElement(root, "device", {"id": other_id, "name": "Fixture peer"})
    ET.SubElement(peer, "address").text = f"tcp://{other_name}:22000"
    gui = root.find("gui")
    for key, value in {
        "address": "0.0.0.0:8384", "apikey": API_KEY,
        "user": "fixture-user", "password": "fixture-password",
    }.items():
        element = gui.find(key)
        if element is None:
            element = ET.SubElement(gui, key)
        element.text = value
    options = root.find("options")
    for element in list(options.findall("listenAddress")):
        options.remove(element)
    ET.SubElement(options, "listenAddress").text = "tcp://0.0.0.0:22000"
    for key, value in {
        "globalAnnounceEnabled": "false", "localAnnounceEnabled": "false",
        "relaysEnabled": "false", "natEnabled": "false",
        "startBrowser": "false", "urAccepted": "-1",
        "crashReportingEnabled": "false", "reconnectionIntervalS": "5",
        "autoUpgradeIntervalH": "0",
    }.items():
        element = options.find(key)
        if element is None:
            element = ET.SubElement(options, key)
        element.text = value
    return ET.tostring(root, encoding="utf-8")


class FixtureTests(unittest.TestCase):
    def test_isolation_and_folder_replacement_preserve_identity(self):
        content = b"""<configuration version="37">
          <folder id="default" path="/unwanted"/>
          <device id="SELF"/><gui><user/><password/><apikey/></gui>
          <options><listenAddress>default</listenAddress>
          <globalAnnounceEnabled>true</globalAnnounceEnabled></options>
        </configuration>"""
        root = ET.fromstring(fixture_config(content, "PEER", "fixture-peer"))
        self.assertEqual(root.find("device").get("id"), "SELF")
        self.assertEqual([f.get("path") for f in root.findall("folder")], ["/data/books"])
        self.assertEqual(
            [d.get("id") for d in root.find("folder").findall("device")],
            ["SELF", "PEER"],
        )
        self.assertEqual(root.findall("device")[1].findtext("address"),
                         "tcp://fixture-peer:22000")
        self.assertEqual([x.text for x in root.find("options").findall("listenAddress")],
                         ["tcp://0.0.0.0:22000"])
        for key in ("globalAnnounceEnabled", "localAnnounceEnabled",
                    "relaysEnabled", "natEnabled", "crashReportingEnabled"):
            self.assertEqual(root.find("options").findtext(key), "false")


class Rehearsal:
    def __init__(self):
        self.prefix = "syncthing-ci-" + uuid.uuid4().hex[:12]
        self.network = self.prefix + "-net"
        self.volumes = []
        self.containers = []
        self.network_created = False
        self.names = {name: self.prefix + "-" + name for name in ("app", "peer", "image")}

    def docker(self, *args, **kwargs):
        return command("docker", *args, **kwargs)

    def mounted(self, image, mounts, *args, input=None):
        argv = ["run", "--rm", "-i", "--network", "none", "--user", "1000:1000",
                "--cap-drop=ALL", "--security-opt=no-new-privileges"]
        for volume, target in mounts:
            argv += ["--mount", f"type=volume,source={volume},target={target}"]
        return self.docker(*argv, "--entrypoint", "/bin/sh", image, "-c", *args, input=input)

    def volume(self, suffix):
        name = self.prefix + "-" + suffix
        self.docker("volume", "create", "--label", f"syncthing.fixture={self.prefix}", name)
        self.volumes.append(name)
        self.docker(
            "run", "--rm", "--network", "none", "--cap-drop=ALL", "--cap-add=CHOWN",
            "--security-opt=no-new-privileges", "--user", "0:0",
            "--mount", f"type=volume,source={name},target=/fixture",
            "--entrypoint", "/bin/chown", OLD_IMAGE, "1000:1000", "/fixture",
        )
        return name

    def generate(self, volume):
        self.docker(
            "run", "--rm", "--network", "none", "--user", "1000:1000",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--mount", f"type=volume,source={volume},target=/config",
            OLD_IMAGE, "generate", "--home=/config",
        )
        return self.mounted(OLD_IMAGE, [(volume, "/config")], "cat /config/config.xml")

    def start(self, role, image, config, books):
        name = self.names[role]
        self.containers.append(name)
        self.docker(
            "run", "-d", "--name", name, "--network", self.network,
            "--user", "1000:1000", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--cpus=0.5", "--memory=512m",
            "--env", "STNOUPGRADE=true", "--env", "TZ=America/Los_Angeles",
            "--mount", f"type=volume,source={config},target=/config",
            "--mount", f"type=volume,source={books},target=/data/books",
            image, *(NEW_ARGS if image == NEW_IMAGE else OLD_ARGS),
        )
        try:
            self.wait(lambda: self.api(role, "/rest/noauth/health")["status"] == "OK",
                      "native health within the existing startup-probe budget", timeout=150)
        except AssertionError:
            logs = self.docker("logs", name).decode(errors="replace")
            classes = [label for label, pattern in {
                "unknown_flag": r"(?i)unknown flag|flag provided but not defined",
                "permission": r"(?i)permission denied|operation not permitted",
                "database": r"(?i)database.*(?:error|failed)|sqlite.*(?:error|failed)",
                "configuration": r"(?i)config.*(?:error|failed|invalid)",
            }.items() if re.search(pattern, logs)]
            raise AssertionError("Native startup failure classes: " + repr(classes)) from None
        self.assert_identity(role)

    def stop(self, role):
        name = self.names[role]
        self.docker("stop", "--time", "30", name)
        state = json.loads(self.docker("inspect", "--format", "{{json .State}}", name))
        assert state["ExitCode"] == 0 and not state["OOMKilled"], "Ungraceful native shutdown"
        self.docker("rm", "-v", name)
        self.containers.remove(name)

    def api(self, role, path):
        return json.loads(self.docker(
            "exec", "--user", "1000:1000", self.names[role], "wget", "-qO-", "-T", "3",
            "--header", "X-API-Key: " + API_KEY, "http://127.0.0.1:8384" + path,
            timeout=10,
        ))

    def assert_identity(self, role):
        assert self.docker("exec", self.names[role], "id", "-u").strip() == b"1000"
        assert self.docker("exec", self.names[role], "id", "-g").strip() == b"1000"
        config = self.api(role, "/rest/config")
        assert [(f["id"], f["path"]) for f in config["folders"]] == [(FOLDER, "/data/books")]
        assert config["gui"]["user"] == "fixture-user"
        assert config["gui"]["apiKey"] == API_KEY

    @staticmethod
    def wait(check, description, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if check():
                    return
            except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired):
                pass
            time.sleep(2)
        raise AssertionError("Timed out: " + description)

    def write(self, role, name, content):
        assert re.fullmatch(r"[a-z0-9-]+\.txt", name)
        self.docker("exec", "-i", "--user", "1000:1000", self.names[role],
                    "/bin/sh", "-c", "cat > /data/books/" + name, input=content)

    def files_match(self, role, files):
        for name, content in files.items():
            if self.docker("exec", self.names[role], "cat", "/data/books/" + name) != content:
                return False
        state = self.api(role, "/rest/db/status?folder=" + FOLDER)
        return state["localFiles"] == len(files) and state["needTotalItems"] == 0

    def public_image_evidence(self):
        spec = importlib.util.spec_from_file_location(
            "syncthing_public_stores", Path(__file__).with_name("test-media-vendor-ca.py")
        )
        scanner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scanner)
        name = self.names["image"]
        self.docker("create", "--name", name, NEW_IMAGE)
        self.containers.append(name)
        with subprocess.Popen(
            ["docker", "export", name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ) as process:
            try:
                stores = scanner.inspect_archive(process.stdout, scanner.DENIED)
                process.stdout.read()
                assert process.wait(timeout=120) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        assert not stores["denied"], "Denied SPKI in candidate public PEM store"
        with tempfile.TemporaryDirectory(prefix=self.prefix) as directory:
            binary = Path(directory) / "syncthing"
            self.docker("cp", name + ":/bin/syncthing", str(binary))
            metadata = command("go", "version", "-m", str(binary)).decode()
        match = re.search(r": (go1\.\d+\.\d+)(?:\s|$|-)", metadata)
        assert match and match[1] == "go1.27.3", "Candidate compiler identity changed"
        return {"go_version": match[1], "public_pem_stores": len(stores["stores"]),
                "denied_spkis": 0}

    def run(self):
        for image in (OLD_IMAGE, NEW_IMAGE):
            for attempt in range(3):
                try:
                    self.docker("pull", "--platform=linux/amd64", image)
                    break
                except RuntimeError:
                    if attempt == 2:
                        raise
                    time.sleep(5)
            metadata = json.loads(self.docker("image", "inspect", image))[0]
            assert metadata["Architecture"] == "amd64"
            assert metadata["Config"]["Entrypoint"] == ["/bin/entrypoint.sh", "/bin/syncthing"]
        report = {"scope": "Synthetic migration only; not production or native/custom TLS certification",
                  "old_image": OLD_IMAGE, "candidate_image": NEW_IMAGE,
                  "trust": self.public_image_evidence()}
        self.docker("network", "create", "--internal", self.network)
        self.network_created = True
        assert json.loads(self.docker("network", "inspect", self.network))[0]["Internal"]
        config, books, peer_config, peer_books, rollback = [
            self.volume(suffix) for suffix in ("config", "books", "peer-config", "peer-books", "rollback")
        ]
        old_xml, peer_xml = self.generate(config), self.generate(peer_config)
        own_id = ET.fromstring(old_xml).find("device").get("id")
        peer_id = ET.fromstring(peer_xml).find("device").get("id")
        for volume, content, other_id, other_role in (
            (config, old_xml, peer_id, "peer"), (peer_config, peer_xml, own_id, "app"),
        ):
            updated = fixture_config(content, other_id, self.names[other_role])
            self.mounted(OLD_IMAGE, [(volume, "/config")], "cat > /config/config.xml", input=updated)
        self.start("peer", OLD_IMAGE, peer_config, peer_books)
        self.start("app", OLD_IMAGE, config, books)
        files = {"first-book.txt": b"Disposable generated book fixture one.\n"}
        self.write("peer", "first-book.txt", files["first-book.txt"])
        self.wait(lambda: self.files_match("app", files), "legacy peer synchronization")
        original_gui = copy.deepcopy(self.api("app", "/rest/config")["gui"])
        original_id = self.api("app", "/rest/system/status")["myID"]
        self.stop("app")
        self.mounted(OLD_IMAGE, [(config, "/source"), (rollback, "/snapshot")],
                     "cp -a /source/. /snapshot/")
        self.start("app", NEW_IMAGE, config, books)
        assert self.api("app", "/rest/system/status")["myID"] == original_id
        assert self.api("app", "/rest/config")["gui"] == original_gui
        self.wait(lambda: self.files_match("app", files), "migrated folder and file content")
        sqlite = self.docker(
            "exec", self.names["app"], "/bin/sh", "-c",
            "find /config -type f | while IFS= read -r f; do "
            'if [ "$(head -c 15 "$f")" = "SQLite format 3" ]; then echo sqlite; fi; done',
        ).splitlines()
        assert sqlite, "No native SQLite database created by the major migration"
        files["second-book.txt"] = b"Disposable post-migration legacy peer fixture.\n"
        self.write("peer", "second-book.txt", files["second-book.txt"])
        self.wait(lambda: self.files_match("app", files), "v1 to v2 synchronization")
        files["third-book.txt"] = b"Disposable post-migration candidate fixture.\n"
        self.write("app", "third-book.txt", files["third-book.txt"])
        self.wait(lambda: self.files_match("peer", files), "v2 to v1 synchronization")
        self.stop("app")
        self.start("app", NEW_IMAGE, config, books)
        self.wait(lambda: self.files_match("app", files), "candidate cold restart")
        assert self.api("app", "/rest/system/status")["myID"] == original_id
        assert self.api("app", "/rest/config")["gui"] == original_gui
        self.stop("app")
        self.start("app", OLD_IMAGE, rollback, books)
        self.wait(lambda: self.files_match("app", files), "rollback using the pre-migration database")
        assert self.api("app", "/rest/system/status")["myID"] == original_id
        assert self.api("app", "/rest/config")["gui"] == original_gui
        self.stop("app")
        self.stop("peer")
        report.update({"native_sqlite_databases": len(sqlite), "identity_and_auth_preserved": True,
                       "bidirectional_legacy_peer_sync": True, "cold_restart": True,
                       "rollback_from_stopped_legacy_snapshot": True,
                       "generated_files_verified": len(files), "deleted_file_retention": "indefinite"})
        return report

    def cleanup(self):
        failures = []
        for name in reversed(self.containers):
            result = subprocess.run(["docker", "rm", "-fv", name], capture_output=True, timeout=60)
            if result.returncode:
                failures.append("container")
        if self.network_created:
            result = subprocess.run(["docker", "network", "rm", self.network],
                                    capture_output=True, timeout=60)
            if result.returncode:
                failures.append("network")
        for volume in reversed(self.volumes):
            result = subprocess.run(["docker", "volume", "rm", volume],
                                    capture_output=True, timeout=60)
            if result.returncode:
                failures.append("volume")
        assert not failures, "Synthetic fixture cleanup failed: " + repr(failures)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(FixtureTests)
    )
    if not result.wasSuccessful():
        raise SystemExit(1)
    if args.docker:
        rehearsal = Rehearsal()
        try:
            evidence = rehearsal.run()
            print(json.dumps(evidence, indent=2))
            if args.evidence:
                args.evidence.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        finally:
            rehearsal.cleanup()
