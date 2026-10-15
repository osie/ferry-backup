import ipaddress
import os
import subprocess
import tempfile


def _run(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _read(path):
    with open(path) as f:
        return f.read()


def expiring(cert, days=60):
    if not cert:
        return True
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "leaf.crt")
        with open(path, "w") as f:
            f.write(cert)
        r = subprocess.run(
            ["openssl", "x509", "-checkend", str(days * 86400), "-noout", "-in", path], capture_output=True
        )
        return r.returncode != 0


def make_ca(common_name, days=3650):
    with tempfile.TemporaryDirectory() as d:
        _run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "ca.key"], d)
        _run([
            "openssl", "req", "-x509", "-new", "-key", "ca.key", "-sha256", "-days", str(days),
            "-subj", f"/O=Ferry/CN={common_name}",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-out", "ca.crt",
        ], d)
        return _read(os.path.join(d, "ca.crt")), _read(os.path.join(d, "ca.key"))


def issue(ca_cert, ca_key, common_name, usage, names=(), days=825):
    sans = []
    for n in names:
        try:
            ipaddress.ip_address(n)
            sans.append(f"IP:{n}")
        except ValueError:
            sans.append(f"DNS:{n}")
    eku = "serverAuth" if usage == "server" else "clientAuth"
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "ca.crt"), "w") as f:
            f.write(ca_cert)
        with open(os.path.join(d, "ca.key"), "w") as f:
            f.write(ca_key)
        ext = [
            "basicConstraints=critical,CA:FALSE",
            "keyUsage=critical,digitalSignature,keyEncipherment",
            f"extendedKeyUsage={eku}",
        ]
        if sans:
            ext.append("subjectAltName=" + ",".join(sorted(set(sans))))
        with open(os.path.join(d, "leaf.ext"), "w") as f:
            f.write("\n".join(ext) + "\n")
        _run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "leaf.key"], d)
        _run(["openssl", "req", "-new", "-key", "leaf.key", "-subj", f"/O=Ferry/CN={common_name}", "-out", "leaf.csr"], d)
        _run([
            "openssl", "x509", "-req", "-in", "leaf.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
            "-CAcreateserial", "-sha256", "-days", str(days), "-extfile", "leaf.ext", "-out", "leaf.crt",
        ], d)
        return _read(os.path.join(d, "leaf.crt")), _read(os.path.join(d, "leaf.key"))
