# Loki storage and memory

Loki uses one single-binary replica and the Longhorn claim `monitoring/storage-loki-0`.
The desired capacity is **30Gi**, with **14-day retention** (`336h`) unchanged.
The memory request is `768Mi`, the limit is `2Gi`, and WAL replay is limited to
`512MB` before flushing. This replay ceiling is not a limit on total Loki memory.
Both StatefulSet PVC retention policies are `Retain`: local filesystem chunks
cannot be recovered from an external object store.

The October 2026 incident exhausted the previous 10Gi filesystem. Loki still
reported ready while chunk flushing, WAL writes, and compactor retention failed
with insufficient space. The previous container also terminated with
`OOMKilled` at a `768Mi` limit. Extra memory alone cannot repair a full disk.
30Gi provides recovery and compaction headroom, not a guarantee that every
ingestion rate fits 14 days. Monitor free space and growth after retention
recovers; Loki filesystem storage does not delete logs merely because a disk
is full.

## Expanding an existing installation

**Do not simply reconcile the size change on an existing 10Gi StatefulSet.**
Kubernetes forbids changing its `volumeClaimTemplates`. A PVC expansion is
online-capable, but changing the template requires recreating the StatefulSet
controller. Do not delete the PVC, scale Loki to zero, force a Helm replacement,
or delete the pod to resize storage.

Chart 7.5.0 has no single-binary `existingClaim` setting. Its optional
`enableStatefulSetRecreationForSizeChange` pre-upgrade hook attempts to orphan
the controller and patch PVCs, but its PVC Role uses API group `v1` rather than
the core API group `""`, and its exec argument `--type='json'` contains literal
quotes rejected by kubectl. Leave that hook disabled; enabling it is not a safe
resize procedure on this chart.

The following is an **operator-approved maintenance procedure**, not an
automatically executed deployment. Confirm the active chart still matches this
analysis if it has upgraded. Obtain authorization before any live mutation,
especially controller deletion and the final single-replica rollout.

1. Inspect the PVC, Longhorn volume and both replicas. Confirm they are healthy,
   expansion is allowed by the StorageClass, and each replica disk can accept
   another 20Gi without crossing scheduling or minimum-free-space thresholds.
   Verify an appropriate backup/recovery point. Record the PVC UID and pod UID.
2. Suspend only the Loki HelmRelease before publishing/reconciling the final
   manifest. Keep it suspended until its desired 30Gi values are present in the
   API; coordinate Git/Flux reconciliation with the other maintenance work.

   ```powershell
   flux suspend helmrelease loki -n monitoring
   ```

3. Protect the existing claim **before** orphaning the controller. The old
   StatefulSet used `Delete` policies. Patch them, then wait for the StatefulSet
   controller to remove its owner reference from the PVC. Do not continue if
   `deletionTimestamp` is present or the old controller still owns the claim.

   ```powershell
   kubectl patch statefulset loki -n monitoring --type=merge --patch '{"spec":{"persistentVolumeClaimRetentionPolicy":{"whenDeleted":"Retain","whenScaled":"Retain"}}}'
   kubectl get pvc storage-loki-0 -n monitoring -o jsonpath='{.metadata.uid}{"\n"}{.metadata.ownerReferences}{"\n"}{.metadata.deletionTimestamp}{"\n"}'
   ```

4. Expand the existing claim without restarting the pod.

   ```powershell
   kubectl patch pvc storage-loki-0 -n monitoring --type=merge --patch '{"spec":{"resources":{"requests":{"storage":"30Gi"}}}}'
   kubectl get pvc storage-loki-0 -n monitoring -w
   ```

   Wait for requested and reported capacity to reach 30Gi, Longhorn expansion to
   finish, and the mounted filesystem to gain space. Inspect PVC conditions and
   events. `FileSystemResizePending` requires investigation; do not automatically
   restart or delete the pod. Confirm flush/WAL failures stop increasing before
   proceeding. The Loki image may not contain `df`; kubelet
   `/stats/summary` includes capacity and free bytes for the `storage` volume.
5. Confirm the suspended HelmRelease now contains the Git-backed 30Gi template,
   `Retain` policies and memory settings. Recheck claim ownership, UID and health.
   **Only with explicit approval**, orphan the controller, preserving its pod,
   then let Helm recreate it and adopt the existing claim/pod.

   ```powershell
   kubectl delete statefulset loki -n monitoring --cascade=orphan --wait=true
   flux resume helmrelease loki -n monitoring
   flux reconcile helmrelease loki -n monitoring --with-source
   kubectl rollout status statefulset/loki -n monitoring --timeout=10m
   ```

   The controller recreation itself preserves the pod. Applying the new memory,
   WAL configuration and storage-size annotation then causes a single-replica
   rolling replacement: expect a brief Loki ingestion/query interruption.
6. Verify HelmRelease `Ready`, pod readiness/restart count, the unchanged PVC
   UID, a 30Gi claim/template/filesystem, and both retention policies. Verify
   `/ready`, a recent-log query without printing log content, successful chunk
   flushing, a successful retention pass, stable WAL/flush-error counters, and
   memory over multiple intervals. Readiness alone did not detect this incident.

### Rollback constraints

Expanded volumes cannot be shrunk. Keep `size: 30Gi` and both `Retain` policies
in any corrective release; do not blindly roll Helm back to the 10Gi revision.
If an upgrade fails, inspect Helm/Flux conditions and roll forward with the
expanded claim preserved. Do not repeatedly delete controllers or restore old
claim templates. Restoring data from backup is a separate, explicitly approved
operation and cannot recover logs already lost while WAL persistence failed.
