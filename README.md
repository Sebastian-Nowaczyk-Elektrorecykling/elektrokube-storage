# Elektrokube storage

See the [2026-10-06 upgrade audit](docs/audit-2026-10-06.md) before merging the
Longhorn/CNPG updates into an automatically reconciled branch.


FluxCD configuration for Longhorn, CloudNativePG and Garage on Elektrokube.
Based on [k8s-addon-storage](https://github.com/Sebastian-Nowaczyk-Elektrorecykling/k8s-addon-storage/tree/f11ddf0910058ff7c336e52fabb18ea9e838d988),
with native CNPG StorageClass defaulting. This repository is a second Git source
beside `elektrokube-cilium-and-flux`; it uses the existing Flux controllers.

| Component | Pinned version | Configuration |
| --- | --- | --- |
| Longhorn | chart/application `1.13.0` | V1 engine, `/var/lib/longhorn`, one replica by default |
| CloudNativePG | chart `0.29.1`, operator `1.30.1` | Cluster-wide operator in `cnpg-system` |
| Garage | chart `0.10.2`, application `2.4.1` | Single node, automatic layout, SQLite, persistent storage |

Garage's official chart is pinned to Git commit
`268334bd2530fa99f8b06c7383b2e9f776691edd`. Its metadata volume is `1Gi` and its
object-data volume is `20Gi`. The chart generates and reuses the RPC secret.
CNPG installs the operator; application databases are created separately.

## Install

On the **original first node**, from an up-to-date
[elektrokube-scripts](https://github.com/Sebastian-Nowaczyk-Elektrorecykling/elektrokube-scripts)
checkout:

```bash
# Complete this once if Cilium and Flux have not yet been handed to GitOps:
sudo ./gitops-cilium-and-flux.sh
sudo ./gitops-storage.sh
```

The storage script uses `/etc/rancher/k3s/k3s.yaml`, checks the existing Flux
controllers and `cilium` Kustomization, then registers
`GitRepository/elektrokube-storage` and `Kustomization/elektrokube-storage` in
`flux-system`. It waits for a fresh reconciliation of the fetched commit and
all storage children. It requires no Flux CLI or GitHub credentials and never
installs Flux. Reruns preserve Git-managed settings and refuse conflicting
ownership or deliberately suspended resources.

Prerequisites:

- Kubernetes **1.36+**, with `admissionregistration.k8s.io/v1` serving
  `MutatingAdmissionPolicy` and `MutatingAdmissionPolicyBinding`. Elektrokube
  currently pins k3s `v1.36.5+k3s1`. The script checks API discovery before writes.
- Existing Flux with source, Kustomize and Helm controllers; the base `cilium`
  Kustomization must be ready.
- At least one schedulable worker/hybrid prepared by `prepare-node.sh`, with
  active iSCSI, NFS clients, mount propagation and sufficient ext4/XFS space at
  `/var/lib/longhorn`. Longhorn uses kubelet root `/var/lib/kubelet`.
- Outbound access to the Git/chart sources and container registries.
- No other default StorageClass. The base disables k3s local-path storage;
  remove any later competing default annotation deliberately before running.

Existing storage owned by a different GitOps repository or Helm installation
requires an explicit migration; this script does not adopt it automatically.

For administrators registering this source through another GitOps root, the
equivalent seed is `kubectl apply -k bootstrap` after checking these prerequisites.
The source reconciles `./clusters/elektrokube`, including its own bootstrap files.

## StorageClasses

All four match the reference: `driver.longhorn.io`, ext4, V1 engine, `Immediate`
binding, `Delete` reclaim policy, volume expansion, 30-minute stale replica
timeout, and disabled data locality. Flux owns the classes; the Helm chart's
automatic StorageClass creation is disabled.

| StorageClass | Longhorn replicas | Selection |
| --- | --- | --- |
| `longhorn` | 1 | Sole Kubernetes default |
| `longhorn-cnpg` | 1 | Admission default for CNPG data and optional WAL storage |
| `longhorn-garage` | 1 | Garage metadata and data PVCs |
| `longhorn-replicated` | 3 | Explicit opt-in; needs three eligible storage nodes |

Replica node anti-affinity is enforced and initially degraded volume creation
is disabled. Single-replica volumes and single-node Garage do not provide
node-loss redundancy. `Delete` means deleting a PVC can delete its underlying
data; Flux's pruning safeguards do not change that behavior. No backup target,
bucket, credentials or schedule is created by this repository.

## CNPG defaulting

`cnpg-default-storage-class` is a native `MutatingAdmissionPolicy` and binding.
It handles CREATE and UPDATE of `postgresql.cnpg.io/v1` `Cluster` resources in
all namespaces. If `spec.storage.storageClass` is absent, it sets
`longhorn-cnpg`, unless `spec.storage.pvcTemplate.storageClassName` already
specifies a class. The same rule applies to `spec.walStorage` **only when that
optional volume is present**. All other fields are preserved; explicit class
names and explicit empty strings are preserved. An empty string deliberately
disables dynamic provisioning. Tablespace storage must be configured explicitly.

This defaults Cluster specifications; it does not move data or modify existing
PVC classes. Before enabling it for existing databases with unspecified classes,
set their intended current class explicitly. Removing an unspecified field on a
later Cluster update causes the default to be applied again.

Application Flux Kustomizations should depend on `storage-cnpg-policy` in
`flux-system`, which waits for both the CNPG operator/CRD and StorageClasses.
The optional [example](examples/postgres.yaml) is outside Flux's paths:

```bash
kubectl create --dry-run=server -f examples/postgres.yaml -o yaml
# Confirm spec.storage.storageClass is longhorn-cnpg before creating a database.
```

## Reconciliation and operations

| Kustomization | Dependencies |
| --- | --- |
| `storage-namespaces`, `storage-sources` | Existing Flux |
| `storage-longhorn` | Namespaces, sources, base `cilium` |
| `storage-classes` | Ready Longhorn release |
| `storage-cnpg` | Namespaces, sources |
| `storage-cnpg-policy` | Ready CNPG and StorageClasses |
| `storage-garage` | StorageClasses, namespaces, sources |

Children wait for their resources, including Helm readiness. Releases retry
failed reconciliations and correct drift. Namespaces, StorageClasses and storage
HelmReleases have `prune: false`; all Kustomizations orphan resources when deleted.
Removing the root therefore does not uninstall storage. Plan manual removals and
data retention separately. Changes to policy/source manifests remain prunable.

```bash
kubectl -n flux-system get gitrepositories,kustomizations
kubectl get helmreleases -A
kubectl get storageclasses
kubectl get mutatingadmissionpolicy,mutatingadmissionpolicybinding cnpg-default-storage-class
kubectl -n longhorn-system port-forward --address 127.0.0.1 svc/longhorn-frontend 8080:80
kubectl -n garage exec garage-0 -- /garage status
```

Garage's internal S3 endpoint is `http://garage.garage.svc.cluster.local:3900`,
region `garage`. Create buckets and access keys through the Garage CLI as needed;
store credentials in Kubernetes Secrets outside Git. No ingress is enabled.

## Validation

```bash
python3 -m pip install -r requirements-dev.txt
# Requires helm 3.19.0, kustomize 5.7.1 and kubeconform 0.7.0 on PATH:
python3 scripts/validate.py
```

Validation builds every Kustomize path, renders the pinned upstream charts,
checks all schemas without skipping unknown kinds, and verifies class settings,
dependency order and Garage initialization. CI also uses an isolated Kubernetes
1.36.4 kind cluster to exercise the actual CEL policy and CNPG CRD without
installing the operator or creating database pods. Live storage acceptance still
requires the prepared Elektrokube nodes.

References: [Flux Kustomizations](https://fluxcd.io/flux/components/kustomize/kustomizations/),
[native mutation policies](https://kubernetes.io/docs/reference/access-authn-authz/mutating-admission-policy/),
[CNPG storage](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.1/docs/src/storage.md).
