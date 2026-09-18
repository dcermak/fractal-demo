"""Generate Helm values from the host gateway configuration and a libvirt network."""

import argparse
import ipaddress
import json
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from fractal_demo.gateway import load_settings


def gateway_address(uri, network, settings):
    result = subprocess.run(
        ["virsh", "--connect", uri, "net-dumpxml", network],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    addresses = [
        ip
        for ip in ET.fromstring(result.stdout).findall("ip")
        if ip.get("family", "ipv4") == "ipv4"
    ]
    if len(addresses) != 1:
        raise ValueError("libvirt network must have one IPv4 address; use --gateway-url instead")
    entry = addresses[0]
    address = ipaddress.IPv4Address(entry.get("address", ""))
    prefix = entry.get("prefix") or entry.get("netmask")
    if prefix is None:
        raise ValueError("libvirt network has no IPv4 prefix or netmask")
    subnet = ipaddress.ip_network(f"{address}/{prefix}", strict=False)
    allowed = [ipaddress.ip_network(network) for network in settings.node_networks]
    if not any(subnet.overlaps(network) for network in allowed):
        raise ValueError("libvirt network does not overlap configured node_networks")
    return str(address)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="TOML file used by the host gateway")
    parser.add_argument("--libvirt-uri", default="qemu:///system")
    parser.add_argument("--network", default="default")
    parser.add_argument("--gateway-url", help="Explicit HTTP origin; skips libvirt discovery")
    parser.add_argument("--image", help="Full image reference; otherwise use the chart default")
    args = parser.parse_args()
    try:
        settings = load_settings(args.config)
        if settings.local_worker_ports:
            raise ValueError("remove local_worker_ports from the Kubernetes gateway configuration")
        origin = args.gateway_url
        if origin is None:
            address = gateway_address(args.libvirt_uri, args.network, settings)
            origin = f"http://{address}:{settings.port}"
        url = urlsplit(origin)
        if (
            url.scheme != "http"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "gateway URL must be an HTTP origin without credentials or extra parts"
            )
        address = ipaddress.ip_address(url.hostname)
        if address not in [ipaddress.ip_address(bind) for bind in settings.bind]:
            raise ValueError(f"gateway address {address} is missing from configured bind addresses")
        if (url.port if url.port is not None else 80) != settings.port:
            raise ValueError("gateway URL port must match the host configuration port")
        host = f"[{address}]" if address.version == 6 else str(address)
        values = {
            "gatewayUrl": f"http://{host}:{settings.port}",
            "worker": {"port": settings.worker_port},
        }
        if args.image is not None:
            if not args.image.strip() or any(char.isspace() for char in args.image):
                raise ValueError("image must be a nonempty image reference without whitespace")
            values["image"] = args.image
    except subprocess.CalledProcessError as error:
        parser.error(f"virsh failed: {error.stderr.strip()}")
    except (OSError, ValueError, ET.ParseError, subprocess.TimeoutExpired) as error:
        parser.error(str(error))
    print(json.dumps(values, indent=2))


if __name__ == "__main__":
    main()
