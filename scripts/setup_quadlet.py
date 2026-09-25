"""Generate and install a rootful Quadlet deployment from published images."""

import argparse
import ipaddress
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from string import Template

TEMPLATE = Path(__file__).resolve().parents[1] / "deploy/quadlet/fractal-gateway.container.in"
CONFIG_DIR = Path("/etc/fractal-demo")
QUADLET_DIR = Path("/etc/containers/systemd")
SERVICE = "fractal-gateway.service"


def image_reference(value):
    if not re.fullmatch(r"[A-Za-z0-9._/:@-]+", value):
        raise argparse.ArgumentTypeError("image reference contains unsupported characters")
    return value


def credential_directory(value):
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
        raise argparse.ArgumentTypeError(
            "cluster directory must be an absolute path without spaces"
        )
    path = Path(value)
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"cluster directory does not exist: {value}")
    return value.rstrip("/")


def port(value):
    try:
        number = int(value)
        if 1 <= number <= 65535:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("port must be between 1 and 65535")


def render(args, template=TEMPLATE):
    address = str(ipaddress.IPv4Address(args.gateway_ip))
    if ipaddress.IPv4Address(address).is_unspecified:
        raise ValueError("gateway IP must identify a host interface")
    network = str(ipaddress.IPv4Network(args.node_network, strict=False))
    bind = list(dict.fromkeys(("127.0.0.1", address)))
    config = (
        f"bind = {json.dumps(bind)}\n"
        f"port = {args.gateway_port}\n"
        f"node_networks = {json.dumps([network])}\n"
        f"worker_port = {args.worker_port}\n"
    )
    values = (
        json.dumps(
            {
                "gatewayUrl": f"http://{address}:{args.gateway_port}",
                "worker": {"port": args.worker_port},
                "image": args.worker_image,
            },
            indent=2,
        )
        + "\n"
    )
    unit = Template(template.read_text()).substitute(
        GATEWAY_IMAGE=args.gateway_image, CLUSTER_DIR=args.cluster_dir
    )
    return config.encode(), values.encode(), unit.encode()


def write_atomic(path, contents, mode):
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(contents)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(files, config_dir=CONFIG_DIR, quadlet_dir=QUADLET_DIR, run=subprocess.run):
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config_dir, 0o700)
    quadlet_dir.mkdir(parents=True, exist_ok=True)
    targets = (
        (config_dir / "config.toml", files[0], 0o600),
        (config_dir / "values.local.json", files[1], 0o600),
        (quadlet_dir / "fractal-gateway.container", files[2], 0o644),
    )
    previous = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
        for path, _, _ in targets
    }
    if any(old is not None for old in previous.values()):
        backup = config_dir / "previous"
        backup.mkdir(mode=0o700, exist_ok=True)
        for path, old in previous.items():
            destination = backup / path.name
            if old is None:
                destination.unlink(missing_ok=True)
            else:
                write_atomic(destination, *old)
    try:
        for path, contents, mode in targets:
            write_atomic(path, contents, mode)
        run(["systemctl", "daemon-reload"], check=True)
        run(["systemctl", "restart", SERVICE], check=True)
    except Exception:
        for path, old in previous.items():
            if old is None:
                path.unlink(missing_ok=True)
            else:
                write_atomic(path, *old)
        run(["systemctl", "daemon-reload"], check=True)
        if previous[quadlet_dir / "fractal-gateway.container"] is not None:
            run(["systemctl", "restart", SERVICE], check=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-ip", required=True, help="demo host IPv4 address reached by pods"
    )
    parser.add_argument(
        "--node-network", required=True, help="CIDR containing worker node addresses"
    )
    parser.add_argument("--cluster-dir", required=True, type=credential_directory)
    parser.add_argument("--gateway-image", required=True, type=image_reference)
    parser.add_argument("--worker-image", required=True, type=image_reference)
    parser.add_argument("--gateway-port", type=port, default=8081)
    parser.add_argument("--worker-port", type=port, default=8080)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run as root to install the system Quadlet")
    try:
        files = render(args)
        install(files)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Setup failed: {error}\n")
    print(f"Started {SERVICE}. Check systemctl status {SERVICE} and journalctl -u {SERVICE}.")
    print("Worker deployment may still be waiting for the kubeconfig or cluster.")


if __name__ == "__main__":
    main()
