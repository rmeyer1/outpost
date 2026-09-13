#!/usr/bin/env python3
"""Assemble an OCI image tar (loadable via `container image load -i`) from a
rootfs tar produced by `container export`.

Usage: make-image.py <rootfs.tar> <name:tag> <output.tar>

This exists because `container build` (buildkit) requires Rosetta, which is
not installed on this host. The rootfs is prepared by running a container
from a base image, installing everything via `container exec`/`container cp`,
then exporting its filesystem.
"""
import gzip
import hashlib
import io
import json
import sys
import tarfile

BASE_ENV = [
    "PATH=/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "PYTHONDONTWRITEBYTECODE=1",
    "PYTHONUNBUFFERED=1",
    "PIP_NO_CACHE_DIR=1",
    "DEBIAN_FRONTEND=noninteractive",
    "LANG=C.UTF-8",
]


def main() -> int:
    rootfs_path, ref, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(rootfs_path, "rb") as f:
        raw = f.read()
    print(f"rootfs: {len(raw)/1e6:.1f} MB", flush=True)

    layer_gz = gzip.compress(raw, compresslevel=4)
    layer_digest = "sha256:" + hashlib.sha256(layer_gz).hexdigest()
    diff_id = "sha256:" + hashlib.sha256(raw).hexdigest()

    config = {
        "architecture": "arm64",
        "os": "linux",
        "config": {
            "Env": BASE_ENV,
            "Entrypoint": ["python3", "/opt/ca/entrypoint.py"],
            "WorkingDir": "/work",
        },
        "rootfs": {"type": "layers", "diff_ids": [diff_id]},
    }
    config_json = json.dumps(config).encode()
    config_digest = "sha256:" + hashlib.sha256(config_json).hexdigest()

    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": len(config_json),
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": layer_digest,
                "size": len(layer_gz),
            }
        ],
    }
    manifest_json = json.dumps(manifest).encode()
    manifest_digest = "sha256:" + hashlib.sha256(manifest_json).hexdigest()

    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": manifest_digest,
                "size": len(manifest_json),
                "platform": {"architecture": "arm64", "os": "linux"},
                "annotations": {
                    "org.opencontainers.image.ref.name": ref,
                    "io.containerd.image.name": ref,
                    "com.apple.containerization.image.name": ref,
                },
            }
        ],
    }

    def add(t, name, data: bytes):
        ti = tarfile.TarInfo(name)
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))

    with tarfile.open(out_path, "w") as t:
        add(t, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        add(t, "index.json", json.dumps(index).encode())
        add(t, "blobs/sha256/" + config_digest[7:], config_json)
        add(t, "blobs/sha256/" + manifest_digest[7:], manifest_json)
        add(t, "blobs/sha256/" + layer_digest[7:], layer_gz)
    print(f"wrote {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
