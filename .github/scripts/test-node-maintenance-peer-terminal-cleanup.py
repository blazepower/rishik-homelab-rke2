#!/usr/bin/env python3
"""Focused simulated retirement and isolated filesystem checks."""

import importlib.util
from contextlib import nullcontext
import json
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock


spec = importlib.util.spec_from_file_location(
    "peer_terminal_cleanup", Path(__file__).with_name("node-maintenance-peer-terminal-cleanup.py"))
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(cleanup, "BASE", self.root).start()
        patch.object(cleanup, "RECOVERY", self.root / "recovery").start()
        patch.object(cleanup, "PEER", self.root / "peer").start()
        patch.object(cleanup, "checked", lambda path, *args, **kwargs: path).start()
        patch.object(cleanup, "sync", lambda path: None).start()
        patch.object(cleanup.os, "O_NOFOLLOW", getattr(os, "O_NOFOLLOW", 0), create=True).start()
        patch.object(cleanup.os, "fsync", lambda fd: None).start()
        if os.name == "nt":
            handles = {}

            def simulated_open(path, flags):
                descriptor = len(handles) + 1
                handles[descriptor] = Path(path)
                return descriptor

            patch.object(cleanup.os, "open", simulated_open).start()
            patch.object(cleanup.os, "fstat", lambda fd: handles[fd].stat()).start()
            patch.object(cleanup.os, "close", lambda fd: None).start()
            patch.object(cleanup.os, "fchmod", lambda fd, mode: None, create=True).start()
        (self.root / "events").mkdir()
        (self.root / "proofs").mkdir()
        (self.root / "recovery").mkdir()
        (self.root / "recovery" / "operator-clock.json").write_bytes(cleanup.canonical(
            {"source_sha256": cleanup.RECOVERY_SHA, "last_epoch": 1795658500.0}))
        patch.object(cleanup.time, "time", return_value=1795658600.0).start()
        self.operation = cleanup.Cleanup.__new__(cleanup.Cleanup)
        self.operation.source = "a" * 64
        self.operation.node = cleanup.WORKER
        self.operation.context = {"source_sha256": self.operation.source, "setup_id": cleanup.SETUP,
                                  "run_id": cleanup.RUN, "node": cleanup.WORKER,
                                  "recovery_sha256": cleanup.RECOVERY_SHA, "terminals": cleanup.TERMINALS}
        self.operation.core = SimpleNamespace(START=1795588445.0377986)

    def phase(self, name, epoch=1795658550.0):
        return {"context": self.operation.context, "phase": name, "observed_epoch": epoch}

    def test_pending_higher_clock_is_not_discarded_on_repeated_promotion_interruption(self):
        path = self.root / "events" / "authorization-removed.json"
        pending = path.with_name(path.name + ".pending")
        body = self.phase("authorization-removed", 1795658590.0)
        pending.write_bytes(cleanup.canonical(body))
        inode = pending.stat().st_ino
        for _ in range(2):
            with patch.object(cleanup.os, "replace", side_effect=OSError("injected promotion interruption")):
                with self.assertRaises(OSError):
                    self.operation.checkpoint("authorization-removed")
            self.assertEqual(pending.stat().st_ino, inode)
            self.assertEqual(json.loads(pending.read_bytes()), body)
        self.assertEqual(self.operation.checkpoint("authorization-removed"), body)
        self.assertEqual(path.stat().st_ino, inode)

    def test_foreign_pending_context_remains_untouched_and_blocks(self):
        path = self.root / "events" / "identity-removed.json.pending"
        body = self.phase("identity-removed")
        body["context"] = {**self.operation.context, "setup_id": "b" * 32}
        data = cleanup.canonical(body)
        path.write_bytes(data)
        with self.assertRaisesRegex(RuntimeError, "phase/source/clock"):
            self.operation.checkpoint("identity-removed")
        self.assertEqual(path.read_bytes(), data)

    def test_pending_signed_proof_watermark_survives_interrupted_republication(self):
        body = {"protocol": "terminal-peer-cleanup/v1", "setup_id": cleanup.SETUP,
                "run_id": cleanup.RUN, "node": cleanup.WORKER,
                "source_sha256": self.operation.source, "terminals": cleanup.TERMINALS,
                "observed_epoch": 1795658590.0, "outcome": "OWNED-PEER-SETUP-CLEANED",
                "original_records_preserved": True, "maintenance_success": False,
                "ssh_validated_without_restart": True}
        path = self.root / "proofs" / (cleanup.digest(cleanup.canonical(body)) + ".pending")
        with patch.object(cleanup.os, "replace", side_effect=OSError("proof interruption")):
            with self.assertRaises(OSError):
                self.operation.preserve_proof(body)
        self.assertTrue(path.exists())
        with patch.object(cleanup.time, "time", return_value=1795658580.0):
            with self.assertRaises(RuntimeError):
                self.operation.clock()
        self.operation.preserve_proof(body)
        self.assertEqual(json.loads(path.with_suffix(".json").read_bytes()), body)

    def test_clock_rollback_against_pending_phase_is_rejected(self):
        (self.root / "events" / "identity-removed.json.pending").write_bytes(
            cleanup.canonical(self.phase("identity-removed", 1795658590.0)))
        with patch.object(cleanup.time, "time", return_value=1795658580.0):
            with self.assertRaises(RuntimeError):
                self.operation.clock()

    def test_controller_does_not_remove_ssh_when_replacement_maintenance_lock_exists(self):
        operation = self.operation
        operation.node = cleanup.SERVER
        operation.peer_binding = Mock()
        operation.verify_terminals = Mock()
        operation.view = Mock()
        operation.view.lock.return_value = {"metadata": {"uid": "replacement"}}
        operation.authorization = {"activated": True, "setup_id": cleanup.SETUP}
        operation.peer = Mock()
        (self.root / "recovery" / "authority.json").write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "Maintenance lock present"):
            operation.preflight()
        operation.peer.remove_authorization.assert_not_called()

    def test_controller_cannot_retire_ssh_without_signed_worker_first_proof(self):
        operation = self.operation
        operation.node = cleanup.SERVER
        operation.peer_binding = Mock()
        operation.verify_terminals = Mock()
        operation.view = Mock()
        operation.view.lock.return_value = None
        operation.verify_signed = Mock(side_effect=RuntimeError("signature rejected"))
        operation.authorization = {"activated": True, "setup_id": cleanup.SETUP}
        operation.peer = Mock()
        (self.root / "recovery" / "authority.json").write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "signature rejected"):
            operation.preflight()
        operation.peer.remove_authorization.assert_not_called()

    def test_changed_terminal_bytes_are_rejected_before_signature_or_ssh(self):
        inbox = self.root / "inbox"
        inbox.mkdir()
        (inbox / "controller-terminal.json").write_bytes(b'{"outcome":"maintenance-succeeded"}')
        self.operation.verify_signed = Mock()
        with self.assertRaisesRegex(RuntimeError, "terminal witness changed"):
            self.operation.verify_terminals()
        self.operation.verify_signed.assert_not_called()

    def test_initial_private_identity_must_be_complete_before_ssh_changes(self):
        folder = self.root / cleanup.SETUP
        folder.mkdir()
        (folder / "identity.pub").write_text("ssh-ed25519 fixture")
        operation = self.operation
        operation.peer = SimpleNamespace(PRIVATE=self.root)
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            operation.private_proof()

    def test_replaced_private_inode_is_retained(self):
        folder = self.root / cleanup.SETUP
        folder.mkdir()
        private = folder / "identity"
        private.write_bytes(b"synthetic fixture, not a key")
        operation = self.operation
        operation.peer = SimpleNamespace(PRIVATE=self.root)
        intent = {"private_files": {"identity": [-1] * 6}}
        with self.assertRaisesRegex(RuntimeError, "inode replaced"):
            operation.private_proof(intent)
        self.assertTrue(private.exists())

    def test_missing_peer_pointer_requires_all_owned_retirement_phases(self):
        operation = self.operation
        operation.core = SimpleNamespace(checked=lambda *args, **kwargs: None)
        operation.event = Mock(return_value=None)
        with self.assertRaisesRegex(RuntimeError, "without durable owned retirement"):
            operation.peer_binding(SimpleNamespace())

    def test_publication_cannot_claim_cleanup_with_private_identity_remaining(self):
        (self.root / cleanup.SETUP).mkdir()
        self.operation.peer = SimpleNamespace(PRIVATE=self.root, PUBLIC=self.root / "peer")
        with self.assertRaisesRegex(RuntimeError, "not fully retired"):
            self.operation.publish()

    def test_interrupted_private_deletion_resumes_only_remaining_owned_files(self):
        operation = self.operation
        private = self.root / "private"
        public = self.root / "public"
        private.mkdir()
        public.mkdir()
        folder = private / cleanup.SETUP
        folder.mkdir()
        for name in ("identity", "identity.pub", "known_hosts"):
            (folder / name).write_text("ssh-ed25519 synthetic")
        (public / "current.json").write_bytes(cleanup.canonical({"setup_id": cleanup.SETUP}))
        operation.value = {"public_key_fingerprint": "synthetic-fingerprint"}
        operation.authorization = {}
        operation.peer = SimpleNamespace(
            PRIVATE=private, PUBLIC=public, setup_lock=nullcontext,
            remove_authorization=Mock(),
            public_key=lambda key: "synthetic-fingerprint",
            known_host=lambda value: b"ssh-ed25519 synthetic")
        operation.view = SimpleNamespace(locked=nullcontext)
        operation.preflight = Mock()
        operation.remove_authorization = Mock()
        operation.publish = Mock(return_value={"outcome": "fixture-cleaned"})
        patch.object(cleanup, "native", return_value=b"ssh-ed25519 synthetic").start()

        def interrupted_sync(path):
            if path == folder:
                raise OSError("interruption after first private unlink")

        with patch.object(cleanup, "sync", side_effect=interrupted_sync):
            with self.assertRaises(OSError):
                operation.cleanup()
        self.assertFalse((folder / "identity").exists())
        self.assertTrue((folder / "identity.pub").exists())
        self.assertTrue((folder / "known_hosts").exists())
        self.assertIsNotNone(operation.event("authorization-removed"))
        self.assertIsNone(operation.event("identity-removed"))
        self.assertEqual(operation.cleanup(), {"outcome": "fixture-cleaned"})
        self.assertFalse(folder.exists())
        self.assertFalse((public / "current.json").exists())
        self.assertTrue(all(operation.event(phase) is not None for phase in cleanup.PHASES))

    def test_restored_pending_intent_checks_inode_before_authorization_mutation(self):
        operation = self.operation
        private = self.root / "private"
        private.mkdir()
        folder = private / cleanup.SETUP
        folder.mkdir()
        for name in ("identity", "identity.pub", "known_hosts"):
            (folder / name).write_text("ssh-ed25519 synthetic")
        operation.value = {"public_key_fingerprint": "synthetic-fingerprint"}
        operation.peer = SimpleNamespace(
            PRIVATE=private, setup_lock=nullcontext,
            public_key=lambda key: "synthetic-fingerprint",
            known_host=lambda value: b"ssh-ed25519 synthetic")
        operation.view = SimpleNamespace(locked=nullcontext)
        operation.preflight = Mock()
        operation.remove_authorization = Mock()
        patch.object(cleanup, "native", return_value=b"ssh-ed25519 synthetic").start()
        proof = operation.private_proof()
        pending = {**self.phase("intent"), "private_files": proof}
        (self.root / "events" / "intent.json.pending").write_bytes(cleanup.canonical(pending))
        replacement = folder / "replacement"
        replacement.write_bytes((folder / "identity").read_bytes())
        replacement.replace(folder / "identity")
        with self.assertRaisesRegex(RuntimeError, "inode replaced"):
            operation.cleanup()
        operation.remove_authorization.assert_not_called()
        self.assertTrue((folder / "identity").exists())

    def test_cli_sets_private_signature_umask_before_cleanup(self):
        sequence = []
        operation = Mock()
        operation.cleanup.return_value = {"outcome": "fixture-cleaned"}
        with patch.object(cleanup.os, "umask", side_effect=lambda mode: sequence.append(("umask", mode))), \
                patch.object(cleanup, "Cleanup", side_effect=lambda source: (
                    sequence.append(("construct", source)), operation)[1]), \
                patch.object(cleanup.sys, "argv", ["helper.py", "cleanup", "--reviewed-source-sha256", "a" * 64]), \
                patch("builtins.print"):
            cleanup.main()
        self.assertEqual(sequence, [("umask", 0o077), ("construct", "a" * 64)])
        operation.cleanup.assert_called_once()

    def test_worker_proof_expiring_during_native_validation_blocks_first_revocation(self):
        operation = self.operation
        operation.node = cleanup.SERVER
        operation.worker_proof = {
            "protocol": "terminal-peer-cleanup/v1", "setup_id": cleanup.SETUP,
            "run_id": cleanup.RUN, "node": cleanup.WORKER,
            "source_sha256": operation.source, "terminals": cleanup.TERMINALS,
            "observed_epoch": 1795658550.0, "outcome": "OWNED-PEER-SETUP-CLEANED",
            "original_records_preserved": True, "maintenance_success": False,
            "ssh_validated_without_restart": True}
        operation.value = {"setup_id": cleanup.SETUP, "expires_epoch": 1795672745}
        operation.authorization = {
            "setup_id": cleanup.SETUP, "activated": True, "expires_epoch": 1795672745,
            "entry": "fixture-entry", "incoming_public_key": "fixture-key",
            "incoming_key_fingerprint": "fixture-fingerprint"}
        clock = [1795658600.0]
        patch.object(cleanup.time, "time", side_effect=lambda: clock[0]).start()
        operation.peer = Mock()
        operation.peer.ssh_files.return_value = (self.root / "key", self.root / "dropin")
        operation.peer.authorized_entry.return_value = "fixture-entry"
        operation.peer.public_key.return_value = "fixture-fingerprint"
        operation.peer.validate_policy.side_effect = lambda *args, **kwargs: clock.__setitem__(
            0, clock[0] + 120)
        with self.assertRaisesRegex(RuntimeError, "expired before mutation"):
            operation.remove_authorization()
        operation.peer.write_new.assert_not_called()
        operation.peer.execute.assert_not_called()

    def test_post_reload_validation_expiration_cannot_write_cleaned_marker(self):
        operation = self.operation
        operation.node = cleanup.SERVER
        operation.worker_proof = {
            "protocol": "terminal-peer-cleanup/v1", "setup_id": cleanup.SETUP,
            "run_id": cleanup.RUN, "node": cleanup.WORKER,
            "source_sha256": operation.source, "terminals": cleanup.TERMINALS,
            "observed_epoch": 1795658550.0, "outcome": "OWNED-PEER-SETUP-CLEANED",
            "original_records_preserved": True, "maintenance_success": False,
            "ssh_validated_without_restart": True}
        operation.value = {"setup_id": cleanup.SETUP, "expires_epoch": 1795672745}
        operation.authorization = {
            "setup_id": cleanup.SETUP, "activated": True, "expires_epoch": 1795672745,
            "entry": "fixture-entry", "incoming_public_key": "fixture-key",
            "incoming_key_fingerprint": "fixture-fingerprint",
            "policy": {"profiles": {"fixture-context": {"authorized_keys": []}}}}
        (self.root / "peer").mkdir()
        (self.root / "peer" / "authorization.revoked.json").write_bytes(cleanup.canonical(
            {"setup_id": cleanup.SETUP,
             "authorization_sha256": cleanup.digest(cleanup.canonical(operation.authorization))}))
        clock = [1795658600.0]
        patch.object(cleanup.time, "time", side_effect=lambda: clock[0]).start()
        operation.peer = Mock()
        operation.peer.ssh_files.return_value = (self.root / "key", self.root / "dropin")
        operation.peer.authorized_entry.return_value = "fixture-entry"
        operation.peer.public_key.return_value = "fixture-fingerprint"
        operation.peer.ssh_contexts.return_value = {"fixture-context": {}}
        operation.peer.dropin_text.return_value = b"fixture-dropin"
        calls = []

        def policy(*args, **kwargs):
            calls.append(True)
            if len(calls) == 3:
                clock[0] += 90
            return "42"

        operation.peer.validate_policy.side_effect = policy
        with self.assertRaisesRegex(RuntimeError, "expired before mutation"):
            operation.remove_authorization()
        operation.peer.execute.assert_called_once_with(["/usr/bin/systemctl", "reload", "ssh.service"])
        operation.peer.write_new.assert_not_called()


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0,
                     "Actual isolated Linux-root filesystem acceptance")
class NativeCheckpointTests(unittest.TestCase):
    def test_real_pending_clock_promotion_preserves_inode_through_two_interruptions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            os.chmod(root, 0o700)
            (root / "events").mkdir(mode=0o700)
            (root / "proofs").mkdir(mode=0o700)
            (root / "recovery").mkdir(mode=0o700)
            previous = root / "recovery" / "operator-clock.json"
            previous.write_bytes(cleanup.canonical(
                {"source_sha256": cleanup.RECOVERY_SHA, "last_epoch": 1795658500.0}))
            previous.chmod(0o600)
            operation = cleanup.Cleanup.__new__(cleanup.Cleanup)
            operation.context = {"source_sha256": "a" * 64, "node": cleanup.WORKER}
            operation.core = SimpleNamespace(START=1795588445.0377986)
            body = {"context": operation.context, "phase": "identity-removed",
                    "observed_epoch": 1795658550.0}
            pending = root / "events" / "identity-removed.json.pending"
            pending.write_bytes(cleanup.canonical(body))
            pending.chmod(0o600)
            inode = pending.stat().st_ino
            with patch.object(cleanup, "BASE", root), patch.object(cleanup, "RECOVERY", root / "recovery"):
                for _ in range(2):
                    with patch.object(cleanup.os, "replace", side_effect=OSError("fixture interruption")):
                        with self.assertRaises(OSError):
                            operation.checkpoint("identity-removed")
                    self.assertEqual(pending.stat().st_ino, inode)
                operation.checkpoint("identity-removed")
                self.assertEqual((root / "events" / "identity-removed.json").stat().st_ino, inode)


if __name__ == "__main__":
    unittest.main()
