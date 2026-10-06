#!/usr/bin/env python3
"""Render and validate the entire add-on without contacting a Kubernetes cluster."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache"
CHARTS = CACHE / "charts"
RENDERED = CACHE / "rendered"
SCHEMAS = CACHE / "schemas"
KUBE_VERSION = "1.36.4"
FLUX_VERSION = "v2.9.6"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args):
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True)
    require(result.returncode == 0, f"{' '.join(map(str, args))}\n{result.stderr}\n{result.stdout}")
    return result.stdout


def documents(text):
    return [doc for doc in yaml.safe_load_all(text) if doc]


def read(path):
    return documents((ROOT / path).read_text())


def download(url, target):
    if not target.exists():
        print(f"Downloading {url}", flush=True)
        request = urllib.request.Request(url, headers={"User-Agent": "storage-addon-validation"})
        with urllib.request.urlopen(request, timeout=90) as response:
            data = response.read()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return target.read_bytes()


def one(docs, kind, name=None):
    found = [d for d in docs if d["kind"] == kind and
             (name is None or d["metadata"]["name"] == name)]
    require(len(found) == 1, f"Expected one {kind}/{name}; found {len(found)}")
    return found[0]


def save_schemas(docs):
    for doc in docs:
        if doc["kind"] != "CustomResourceDefinition":
            continue
        spec = doc["spec"]
        for version in spec["versions"]:
            schema = version.get("schema", {}).get("openAPIV3Schema")
            if not schema:
                continue
            filename = f'{spec["group"]}_{spec["names"]["kind"].lower()}_{version["name"]}.json'
            (SCHEMAS / filename).write_text(json.dumps(schema))


def invariants(resources, rendered):
    classes = [copy.deepcopy(d) for d in resources if d["kind"] == "StorageClass"]
    require(len(classes) == 4, "Exactly four StorageClasses must be managed")
    classes.sort(key=lambda d: d["metadata"]["name"])
    for sc, name, replicas, default in zip(
            classes, ("longhorn", "longhorn-cnpg", "longhorn-garage", "longhorn-replicated"),
            ("1", "1", "1", "3"), ("true", "false", "false", "false")):
        require(sc["metadata"].pop("name") == name, "Unexpected StorageClass")
        require(sc["metadata"]["annotations"].pop("storageclass.kubernetes.io/is-default-class") == default,
                "Incorrect default StorageClass")
        require(sc["parameters"].pop("numberOfReplicas") == replicas, "Incorrect replica count")
    require(all(sc == classes[0] for sc in classes[1:]),
            "StorageClasses differ beyond name, default, and replicas")
    require(classes[0]["provisioner"] == "driver.longhorn.io", "Wrong provisioner")

    releases = {d["metadata"]["name"]: d for d in resources if d["kind"] == "HelmRelease"}
    require(set(releases) == {"longhorn", "cloudnative-pg", "garage"},
            "Missing or extra Helm releases")
    for name, release in releases.items():
        for action in ("install", "upgrade"):
            policy = release["spec"][action]
            require(policy.get("strategy", {}).get("name") == "RetryOnFailure" and
                    "remediation" not in policy,
                    f"{name}: retry in place instead of uninstalling/rolling back stateful infrastructure")
    lh = releases["longhorn"]["spec"]["values"]
    require(lh["persistence"]["createStorageClass"] is False, "Longhorn chart must not own classes")
    require(lh["defaultSettings"]["defaultReplicaCount"] == 1, "Wrong global replica default")
    require(lh["defaultSettings"]["v2DataEngine"] is False, "Base hosts are prepared for V1")
    require(lh["defaultSettings"]["replicaSoftAntiAffinity"] is False, "Replicas need separate nodes")
    require(lh["defaultSettings"]["allowVolumeCreationWithDegradedAvailability"] is False,
            "New replicated volumes must have capacity for all replicas")
    require(not lh["defaultBackupStore"]["backupTarget"], "Longhorn backup target must remain unset")
    require(not any(d["metadata"]["name"] == "longhorn-storageclass" for d in rendered["longhorn"]),
            "Chart still renders its own StorageClass configuration")

    example = one(read("examples/postgres.yaml"), "Cluster", "example")
    require("storageClass" not in example["spec"]["storage"], "Example must exercise CNPG defaulting")
    policy = one(resources, "MutatingAdmissionPolicy", "cnpg-default-storage-class")
    binding = one(resources, "MutatingAdmissionPolicyBinding", "cnpg-default-storage-class")
    require(policy["apiVersion"] == "admissionregistration.k8s.io/v1", "Stable mutation API required")
    require(binding["spec"]["policyName"] == policy["metadata"]["name"], "Policy binding mismatch")
    require(policy["spec"]["failurePolicy"] == "Fail", "Defaulting must fail closed")
    require(policy["spec"]["reinvocationPolicy"] == "IfNeeded", "Mutation must be idempotent")
    rule = policy["spec"]["matchConstraints"]["resourceRules"]
    require(len(rule) == 1 and rule[0]["apiGroups"] == ["postgresql.cnpg.io"] and
            rule[0]["resources"] == ["clusters"] and rule[0]["apiVersions"] == ["v1"] and
            set(rule[0]["operations"]) == {"CREATE", "UPDATE"}, "Mutation scope is not CNPG Clusters")

    garage = one(rendered["garage"], "StatefulSet", "garage")
    require(garage["spec"]["replicas"] == 1, "Single-node baseline must run one Garage pod")
    container = garage["spec"]["template"]["spec"]["containers"][0]
    require("--single-node" in container["args"], "Garage layout must initialize automatically")
    require(container["readinessProbe"]["httpGet"]["path"] == "/health", "Garage readiness missing")
    claims = garage["spec"]["volumeClaimTemplates"]
    require(len(claims) == 2 and all(c["spec"]["storageClassName"] == "longhorn-garage" for c in claims),
            "Garage metadata and objects must default to longhorn-garage")
    # Parse generated TOML so invalid interpolation cannot pass a YAML-only check.
    import tomllib
    config = tomllib.loads(one(rendered["garage"], "ConfigMap", "garage-config")["data"]["garage.toml"])
    require(config["replication_factor"] == 1, "Garage single-node replication mismatch")

    forbidden = {"Backup", "ScheduledBackup", "Schedule", "BackupStorageLocation",
                 "VolumeSnapshotLocation", "ObjectStore", "RecurringJob", "VolumeSnapshotClass"}
    for doc in resources + [d for docs in rendered.values() for d in docs]:
        require(doc["kind"] not in forbidden, f'Unexpected active backup resource: {doc["kind"]}')
    require(not any(d["kind"] in {"Ingress", "HTTPRoute"} for d in resources), "No external exposure expected")

    ks = {d["metadata"]["name"]: d for d in resources if
          d["kind"] == "Kustomization" and d["apiVersion"].startswith("kustomize.toolkit")}
    external = {"cilium"}
    require(external.isdisjoint(ks), "Do not own the base repository's Kustomizations")
    for name, obj in ks.items():
        require(obj["spec"]["sourceRef"]["name"] == "elektrokube-storage", "Wrong Git source")
        path = ROOT / obj["spec"]["path"]
        require(path.is_dir(), f"Missing Flux target: {path}")
        for dep in obj["spec"].get("dependsOn", []):
            require(dep["name"] in ks or dep["name"] in external, f"Unknown dependency: {dep}")
    visiting, done = set(), set()

    def visit(name):
        if name in done or name in external:
            return
        require(name not in visiting, f"Dependency cycle at {name}")
        visiting.add(name)
        for dep in ks[name]["spec"].get("dependsOn", []):
            visit(dep["name"])
        visiting.remove(name)
        done.add(name)

    for name in ks:
        visit(name)
    for name, required in {
        "storage-longhorn": {"storage-namespaces", "storage-sources", "cilium"},
        "storage-classes": {"storage-longhorn"},
        "storage-cnpg-policy": {"storage-cnpg", "storage-classes"},
        "storage-garage": {"storage-classes"},
    }.items():
        deps = {dep["name"] for dep in ks[name]["spec"].get("dependsOn", [])}
        require(required <= deps, f"Missing startup dependency for {name}")


def main():
    for tool in ("helm", "kustomize", "kubeconform"):
        require(shutil.which(tool), f"Install {tool} and put it on PATH")
    for directory in (CHARTS, RENDERED, SCHEMAS):
        directory.mkdir(parents=True, exist_ok=True)
    resources = {}
    for path in sorted(ROOT.rglob("kustomization.yaml")):
        if any(part.startswith(".") for part in path.relative_to(ROOT).parts):
            continue
        output = run("kustomize", "build", str(path.parent))
        target = RENDERED / (str(path.parent.relative_to(ROOT)).replace("/", "-") + ".yaml")
        target.write_text(output)
        for doc in documents(output):
            key = (doc["apiVersion"], doc["kind"], doc["metadata"].get("namespace", ""), doc["metadata"]["name"])
            require(key not in resources or resources[key] == doc, f"Conflicting resource: {key}")
            resources[key] = doc
    docs = list(resources.values())
    sources = {(d["kind"], d["metadata"]["name"]): d for d in docs if
               d["kind"] in ("HelmRepository", "GitRepository")}
    rendered = {}
    for hr in [d for d in docs if d["kind"] == "HelmRelease"]:
        name, spec = hr["metadata"]["name"], hr["spec"]
        chart_spec = spec["chart"]["spec"]
        ref = chart_spec["sourceRef"]
        source = sources[(ref["kind"], ref["name"])]
        if ref["kind"] == "HelmRepository":
            chart, version = chart_spec["chart"], chart_spec["version"]
            require(re.fullmatch(r"\d+\.\d+\.\d+", version), f"Unpinned chart {name}")
            location = CHARTS / f"{chart}-{version}.tgz"
            if not location.exists():
                print(f"Fetching chart {chart} {version}", flush=True)
                run("helm", "pull", chart, "--version", version, "--repo", source["spec"]["url"],
                    "--destination", str(CHARTS))
        else:
            commit = source["spec"]["ref"]["commit"]
            require(re.fullmatch(r"[a-f0-9]{40}", commit), "Garage source must use an immutable commit")
            location = CHARTS / f"garage-{commit}"
            if not (location / "Chart.yaml").exists():
                repo = source["spec"]["url"].removesuffix(".git")
                data = download(f"{repo}/archive/{commit}.tar.gz", CHARTS / f"garage-{commit}.tar.gz")
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                    for member in archive.getmembers():
                        parts = Path(member.name).parts
                        if tuple(parts[1:4]) != ("script", "helm", "garage") or not member.isfile():
                            continue
                        relative = Path(*parts[4:])
                        require(".." not in relative.parts and not relative.is_absolute(), "Unsafe archive path")
                        target = location / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(archive.extractfile(member).read())
        values = RENDERED / f"{name}-values.yaml"
        values.write_text(yaml.safe_dump(spec.get("values", {})))
        output = run("helm", "template", spec["releaseName"], str(location),
                     "--namespace", hr["metadata"]["namespace"], "--kube-version", KUBE_VERSION,
                     "--include-crds", "--values", str(values))
        rendered[name] = documents(output)
        (RENDERED / f"{name}-chart.yaml").write_text(output)
        save_schemas(rendered[name])
        print(f"Rendered {name}: {len(rendered[name])} resources", flush=True)

    invariants(docs, rendered)
    print("Storage classes, policy scope, dependencies and Garage initialization checks passed", flush=True)
    for name, version, url in (
        ("flux", FLUX_VERSION, f"https://github.com/fluxcd/flux2/releases/download/{FLUX_VERSION}/install.yaml"),
    ):
        data = download(url, CACHE / f"{name}-{version}-crds.yaml")
        save_schemas(documents(data.decode()))
    # Use official schemas for CRDs and native admission policies, including
    # kinds missing from kubeconform's community catalog. Never skip unknowns.
    for group, kinds in {
        "apiextensions.k8s.io": {
            "CustomResourceDefinition": "io.k8s.apiextensions-apiserver.pkg.apis.apiextensions.v1.CustomResourceDefinition"},
        "admissionregistration.k8s.io": {
            kind: "io.k8s.api.admissionregistration.v1." + kind
            for kind in ("MutatingAdmissionPolicy", "MutatingAdmissionPolicyBinding")},
    }.items():
        api = json.loads(download(
            f"https://raw.githubusercontent.com/kubernetes/kubernetes/v{KUBE_VERSION}/"
            f"api/openapi-spec/v3/apis__{group}__v1_openapi.json",
            CACHE / f"{group}-{KUBE_VERSION}.json"))
        definitions = json.loads(json.dumps(api["components"]["schemas"]).replace(
            "#/components/schemas/", "#/definitions/"))
        for kind, definition in kinds.items():
            schema = {"$ref": "#/definitions/" + definition, "definitions": definitions}
            (SCHEMAS / f"{group}_{kind.lower()}_v1.json").write_text(json.dumps(schema))
    all_docs = docs + [d for group in rendered.values() for d in group] + read("examples/postgres.yaml")
    manifest = RENDERED / "all.yaml"
    manifest.write_text(yaml.safe_dump_all(all_docs, sort_keys=False))
    output = run("kubeconform", "-strict", "-summary", "-kubernetes-version", KUBE_VERSION,
                 "-schema-location", str(SCHEMAS / "{{.Group}}_{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"),
                 "-schema-location", "default", str(manifest))
    print(output, end="")
    print("Validation passed. Live cluster acceptance checks are still required.")


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
