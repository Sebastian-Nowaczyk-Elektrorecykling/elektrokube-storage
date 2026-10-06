#!/usr/bin/env python3
"""Exercise CNPG CEL mutation on a disposable kind/envtest API server."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "storage-policy-test"


def kube(*args, obj=None):
    result = subprocess.run(["kubectl", *args], input=json.dumps(obj) if obj is not None else None,
                            text=True, capture_output=True)
    if result.returncode:
        # Preserve API-server diagnostics in CI instead of only a Python traceback.
        print(result.stderr, file=sys.stderr, end="")
        result.check_returncode()
    return result.stdout


def cluster(name, storage=None, wal=None):
    obj = {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
           "metadata": {"name": name, "namespace": NAMESPACE},
           "spec": {"instances": 1, "storage": storage if storage is not None else {"size": "1Gi"}}}
    if wal is not None:
        obj["spec"]["walStorage"] = wal
    return obj


def admit(obj, verb="create"):
    return json.loads(kube(verb, "--dry-run=server", "-f", "-", "-o", "json", obj=obj))["spec"]


def main():
    if kube("config", "current-context").strip() not in {"kind-storage-policy", "envtest-storage-policy"}:
        raise RuntimeError("Run only on the disposable storage-policy kind/envtest cluster.")
    chart = list(yaml.safe_load_all((ROOT / ".cache/rendered/cloudnative-pg-chart.yaml").read_text()))
    crd = next(d for d in chart if d and d["kind"] == "CustomResourceDefinition" and
               d["metadata"]["name"] == "clusters.postgresql.cnpg.io")
    kube("apply", "--server-side", "-f", "-", obj=crd)
    kube("wait", "--for=condition=Established", "crd/clusters.postgresql.cnpg.io", "--timeout=60s")
    kube("create", "namespace", NAMESPACE)
    kube("apply", "-k", str(ROOT / "infrastructure/cnpg-policy"))
    # Admission configuration is observed asynchronously by the API server.
    for attempt in range(30):
        if admit(cluster("probe"))["storage"].get("storageClass") == "longhorn-cnpg":
            break
        time.sleep(1)
    else:
        raise AssertionError("Policy never began defaulting requests")

    for label, storage, expected in (
        ("omitted", {"size": "1Gi"}, "longhorn-cnpg"),
        ("null", {"size": "1Gi", "storageClass": None}, "longhorn-cnpg"),
        ("explicit", {"size": "1Gi", "storageClass": "longhorn-replicated"}, "longhorn-replicated"),
        ("empty", {"size": "1Gi", "storageClass": ""}, ""),
        ("template", {"size": "1Gi", "pvcTemplate": {"storageClassName": "other"}}, None),
        ("empty-template", {"size": "1Gi", "pvcTemplate": {"storageClassName": ""}}, None),
        ("template-without-class", {"size": "1Gi", "pvcTemplate": {"accessModes": ["ReadWriteOnce"]}}, "longhorn-cnpg"),
        ("both-explicit", {"size": "1Gi", "storageClass": "selected", "pvcTemplate": {"storageClassName": "template"}}, "selected"),
    ):
        original = cluster(label, storage=storage, wal=copy.deepcopy(storage))
        mutated = admit(original)
        for field in ("storage", "walStorage"):
            actual = mutated[field]
            assert actual.get("storageClass") == expected, (label, field, actual)
            assert actual["size"] == "1Gi", (label, field, actual)
            if "pvcTemplate" in storage:
                for key, value in storage["pvcTemplate"].items():
                    assert actual["pvcTemplate"][key] == value
        # Reinvocation / resubmitting a defaulted object is stable.
        original["spec"] = mutated
        assert admit(original) == mutated, label
        print(f"PASS: {label}, PGDATA and WAL", flush=True)

    defaulted = admit(cluster("data-only"))
    assert "walStorage" not in defaulted, defaulted
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "unrelated", "namespace": NAMESPACE},
          "data": {"storageClass": "untouched"}}
    assert json.loads(kube("create", "--dry-run=server", "-f", "-", "-o", "json", obj=cm))["data"] == cm["data"]

    saved = json.loads(kube("create", "-f", "-", "-o", "json", obj=cluster("update")))
    saved["spec"]["storage"].pop("storageClass")
    assert admit(saved, "replace")["storage"]["storageClass"] == "longhorn-cnpg"
    saved["spec"]["storage"]["storageClass"] = "chosen-on-update"
    assert admit(saved, "replace")["storage"]["storageClass"] == "chosen-on-update"
    print("PASS: optional WAL, unrelated objects, UPDATE default and explicit override", flush=True)


if __name__ == "__main__":
    main()
