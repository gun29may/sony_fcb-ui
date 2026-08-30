"""Local-network helpers: which addresses this machine can be reached on, and
the URLs to hand out for the GUI and the video stream.
"""
from __future__ import annotations

import socket

import psutil

# Interfaces that are not "the local network" - container bridges, VPN and
# virtual adapters. Matched as name prefixes.
VIRTUAL_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "vmnet", "tun", "tap",
                    "wg", "zt", "tailscale", "cni", "flannel")


def _is_virtual(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in VIRTUAL_PREFIXES)


def _rank(name: str, ip: str) -> int:
    """Higher is more likely to be the address people should actually use."""
    score = 0
    if ip.startswith("192.168."):
        score += 30           # the usual home/office LAN
    elif ip.startswith("10."):
        score += 20
    else:
        score += 10           # 172.16-31 and anything else private
    if name.startswith(("wl", "wlan")):
        score += 5            # wifi
    elif name.startswith(("en", "eth")):
        score += 6            # wired, slightly preferred for video
    if name.startswith("enx"):
        score -= 4            # usb tether / dongle, usually not the LAN
    return score


def interfaces(include_virtual: bool = False) -> list[dict]:
    """Up interfaces with an IPv4 address, best candidate first."""
    stats = psutil.net_if_stats()
    found = []
    for name, addrs in psutil.net_if_addrs().items():
        if not include_virtual and _is_virtual(name):
            continue
        link = stats.get(name)
        if link is not None and not link.isup:
            continue
        for addr in addrs:
            if addr.family != socket.AF_INET or addr.address.startswith("127."):
                continue
            found.append({
                "interface": name,
                "ip": addr.address,
                "netmask": addr.netmask,
                "speed_mbps": link.speed if link else 0,
                "virtual": _is_virtual(name),
                "score": _rank(name, addr.address),
            })
    return sorted(found, key=lambda i: -i["score"])


def primary_ip() -> str | None:
    """Best guess at the address other machines should use."""
    found = interfaces()
    if found:
        return found[0]["ip"]
    # Fall back to whichever source address the routing table would pick.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))       # TEST-NET-1, no packet is sent
            return sock.getsockname()[0]
    except OSError:
        return None


def summary(port: int, bound_host: str, token: str | None = None) -> dict:
    """Everything the GUI needs to tell people how to reach this server."""
    listening_everywhere = bound_host in ("0.0.0.0", "::", "")
    suffix = f"?token={token}" if token else ""
    addrs = interfaces()
    urls = []
    if listening_everywhere:
        for addr in addrs:
            base = f"http://{addr['ip']}:{port}"
            urls.append({
                "interface": addr["interface"],
                "ip": addr["ip"],
                "gui": base + "/" + suffix,
                "stream": f"{base}/stream.mjpg{suffix}",
                "snapshot": f"{base}/snapshot.jpg{suffix}",
            })
    local = f"http://localhost:{port}"
    return {
        "hostname": socket.gethostname(),
        "bound_host": bound_host,
        "port": port,
        "shared": listening_everywhere,
        "protected": bool(token),
        "local": {"gui": local + "/" + suffix, "stream": f"{local}/stream.mjpg{suffix}"},
        "urls": urls,
        "interfaces": addrs,
    }
