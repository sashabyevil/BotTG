"""
Сеть для веб-панели: HTTPS, автоматическое открытие порта на роутере (UPnP)
и правило в брандмауэре Windows. Запускается из bot.py вместе с ботом.

Настройки лежат в server_config.json (создаётся при первом запуске).
По умолчанию всё выключено: панель доступна только на этом компьютере.
Только стандартная библиотека, кроме HTTPS: для сертификата нужен пакет cryptography.
"""

import atexit
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

CONFIG_NAME = "server_config.json"

DEFAULT_CONFIG = {
    # false — панель только на этом компьютере (127.0.0.1)
    # true  — панель слушает все сетевые интерфейсы (0.0.0.0)
    "public": False,
    "port": 5000,
    # true — панель работает по https:// (сертификат создастся сам, если файлов нет)
    "https": False,
    "cert_file": "cert.pem",
    "key_file": "key.pem",
    # true — сам открыть порт на роутере через UPnP
    "upnp": False,
    # порт снаружи; 0 — такой же, как port
    "external_port": 0,
    # true — добавить правило в брандмауэр Windows (нужен запуск от администратора)
    "firewall": False,
}

RULE_NAME = "TTK CORP panel"
SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
)


def log(text):
    print(f"[NET] {text}", flush=True)


# =========================
# КОНФИГ
# =========================

def load_config(base_dir):
    path = Path(base_dir) / CONFIG_NAME
    cfg = dict(DEFAULT_CONFIG)

    if not path.exists():
        try:
            path.write_text(json.dumps(DEFAULT_CONFIG, indent=4, ensure_ascii=False), encoding="utf-8")
            log(f"Создан {CONFIG_NAME}. Чтобы открыть панель в сеть, включите нужные пункты и перезапустите бота.")
        except OSError as e:
            log(f"Не удалось создать {CONFIG_NAME}: {e}")
        return cfg

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("ожидался JSON-объект")
    except Exception as e:
        log(f"{CONFIG_NAME} не прочитан ({e}), использую настройки по умолчанию.")
        return cfg

    for key, default in DEFAULT_CONFIG.items():
        if key not in data:
            continue
        value = data[key]
        if isinstance(default, bool):
            cfg[key] = bool(value)
        elif isinstance(default, int):
            try:
                cfg[key] = int(value)
            except (TypeError, ValueError):
                log(f"Неверное значение {key}={value!r}, беру {default}.")
        else:
            cfg[key] = str(value)

    if not (1 <= cfg["port"] <= 65535):
        log(f"Неверный порт {cfg['port']}, беру {DEFAULT_CONFIG['port']}.")
        cfg["port"] = DEFAULT_CONFIG["port"]
    if not (0 <= cfg["external_port"] <= 65535):
        cfg["external_port"] = 0
    return cfg


# =========================
# АДРЕСА
# =========================

def local_ip(target="10.255.255.255"):
    """IP этого компьютера в локальной сети (пакеты при этом не отправляются)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# =========================
# HTTPS
# =========================

def ensure_certificate(cert_path, key_path, names):
    """Создаёт самоподписанный сертификат, если файлов ещё нет. True — файлы готовы."""
    cert_path, key_path = Path(cert_path), Path(key_path)
    if cert_path.exists() and key_path.exists():
        return True
    if cert_path.exists() != key_path.exists():
        log("Есть только один из файлов сертификата (cert/key). Положите оба или удалите оба.")
        return False

    try:
        import datetime
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        log("Для HTTPS нужен пакет cryptography: py -3.13 -m pip install cryptography")
        return False

    sans, seen = [], set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            sans.append(x509.DNSName(name))

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TTK CORP panel")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log("Создан самоподписанный сертификат (браузер покажет предупреждение — это нормально, см. README).")
    return True


def make_ssl_context(cert_path, key_path):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx


# =========================
# UPnP (открытие порта на роутере)
# =========================

class UpnpError(Exception):
    def __init__(self, code, text):
        super().__init__(f"{code}: {text}" if code else text)
        self.code = code


def _ssdp_find_location(timeout=3.0):
    """Ищет роутер в сети, возвращает URL его описания (LOCATION) или None."""
    for target in SEARCH_TARGETS:
        message = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 2\r\n"
            f"ST: {target}\r\n\r\n"
        ).encode("ascii")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(timeout)
        try:
            sock.sendto(message, SSDP_ADDR)
            while True:
                try:
                    data, _ = sock.recvfrom(4096)
                except socket.timeout:
                    break
                match = re.search(rb"(?im)^location:\s*(\S+)", data)
                if match:
                    return match.group(1).decode("ascii", "ignore")
        except OSError:
            pass
        finally:
            sock.close()
    return None


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]


def _find_wan_service(location):
    """Из описания роутера достаёт (control_url, service_type) для WANIP/WANPPP."""
    with urllib.request.urlopen(location, timeout=5) as resp:
        root = ET.fromstring(resp.read())

    base = location
    for el in root.iter():
        if _local_name(el.tag) == "URLBase" and (el.text or "").strip():
            base = el.text.strip()

    for service in root.iter():
        if _local_name(service.tag) != "service":
            continue
        info = {_local_name(c.tag): (c.text or "").strip() for c in service}
        stype = info.get("serviceType", "")
        if "WANIPConnection" in stype or "WANPPPConnection" in stype:
            return urljoin(base, info["controlURL"]), stype
    return None, None


def _soap(control_url, service_type, action, args=()):
    body_args = "".join(f"<{k}>{v}</{k}>" for k, v in args)
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service_type}">{body_args}</u:{action}></s:Body>'
        "</s:Envelope>"
    ).encode("utf-8")
    req = urllib.request.Request(
        control_url, data=envelope, method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service_type}#{action}"',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            return resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        code = re.search(r"<(?:\w+:)?errorCode>(\d+)<", raw)
        desc = re.search(r"<(?:\w+:)?errorDescription>([^<]*)<", raw)
        raise UpnpError(code.group(1) if code else str(e.code), desc.group(1) if desc else "ошибка SOAP")


class PortMapping:
    def __init__(self, control_url, service_type, internal_ip, internal_port, external_port):
        self.control_url = control_url
        self.service_type = service_type
        self.internal_ip = internal_ip
        self.internal_port = internal_port
        self.external_port = external_port
        self.lease = 0
        self.external_ip = None
        self._stop = threading.Event()

    def _add(self, lease):
        _soap(self.control_url, self.service_type, "AddPortMapping", [
            ("NewRemoteHost", ""),
            ("NewExternalPort", self.external_port),
            ("NewProtocol", "TCP"),
            ("NewInternalPort", self.internal_port),
            ("NewInternalClient", self.internal_ip),
            ("NewEnabled", 1),
            ("NewPortMappingDescription", RULE_NAME),
            ("NewLeaseDuration", lease),
        ])
        self.lease = lease

    def open(self):
        try:
            self._add(0)                      # бессрочно
        except UpnpError as first:
            if first.code == "718":
                raise UpnpError("718", f"порт {self.external_port} на роутере уже занят другим устройством")
            try:
                self._add(3600)               # некоторые роутеры не принимают бессрочные записи
            except UpnpError:
                raise first
            threading.Thread(target=self._renew_loop, daemon=True).start()

        try:
            reply = _soap(self.control_url, self.service_type, "GetExternalIPAddress")
            m = re.search(r"<NewExternalIPAddress>([^<]*)<", reply)
            self.external_ip = m.group(1).strip() if m else None
        except UpnpError:
            self.external_ip = None
        atexit.register(self.close)

    def _renew_loop(self):
        while not self._stop.wait(self.lease * 0.5):
            try:
                self._add(self.lease)
            except Exception as e:
                log(f"Не удалось продлить порт на роутере: {e}")

    def close(self):
        self._stop.set()
        try:
            _soap(self.control_url, self.service_type, "DeletePortMapping", [
                ("NewRemoteHost", ""),
                ("NewExternalPort", self.external_port),
                ("NewProtocol", "TCP"),
            ])
            log(f"Порт {self.external_port} закрыт на роутере.")
        except Exception:
            pass


def open_upnp_port(internal_port, external_port):
    """Возвращает PortMapping или None (причина выводится в консоль)."""
    log("Ищу роутер (UPnP)...")
    location = _ssdp_find_location()
    if not location:
        log("Роутер с UPnP не найден. Включите UPnP в настройках роутера или пробросьте порт вручную.")
        return None

    try:
        control_url, service_type = _find_wan_service(location)
    except Exception as e:
        log(f"Не удалось прочитать описание роутера: {e}")
        return None
    if not control_url:
        log("Роутер не предоставляет сервис проброса портов.")
        return None

    router_host = urlparse(location).hostname
    mapping = PortMapping(control_url, service_type, local_ip(router_host), internal_port, external_port)
    try:
        mapping.open()
    except UpnpError as e:
        log(f"Роутер отказал в открытии порта ({e}).")
        return None
    except Exception as e:
        log(f"Ошибка при открытии порта: {e}")
        return None
    return mapping


# =========================
# БРАНДМАУЭР WINDOWS
# =========================

def _is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _netsh(*args):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        ["netsh", "advfirewall", "firewall", *args],
        capture_output=True, text=True, errors="ignore", creationflags=flags,
    )


def open_firewall_port(port):
    if os.name != "nt":
        log("Правило брандмауэра поддерживается только в Windows, пропускаю.")
        return False

    name = f"{RULE_NAME} {port}"
    if _netsh("show", "rule", f"name={name}").returncode == 0:
        return True

    if not _is_admin():
        log(f"Нет правил брандмауэра для порта {port}. Запустите PowerShell от администратора "
            "один раз или выполните вручную:")
        log(f'  netsh advfirewall firewall add rule name="{name}" dir=in action=allow protocol=TCP localport={port}')
        return False

    result = _netsh("add", "rule", f"name={name}", "dir=in", "action=allow",
                    "protocol=TCP", f"localport={port}", "profile=any")
    if result.returncode == 0:
        log(f"Порт {port} открыт в брандмауэре Windows.")
        return True
    log(f"Не удалось добавить правило брандмауэра: {result.stdout.strip() or result.stderr.strip()}")
    return False


# =========================
# ГЛАВНАЯ ФУНКЦИЯ
# =========================

class NetSettings:
    def __init__(self):
        self.host = "127.0.0.1"
        self.port = DEFAULT_CONFIG["port"]
        self.ssl_context = None
        self.public_url = None

    @property
    def https(self):
        return self.ssl_context is not None


def prepare(base_dir):
    """Читает настройки, открывает порты, готовит HTTPS. Не бросает исключений."""
    base_dir = Path(base_dir)
    net = NetSettings()

    try:
        cfg = load_config(base_dir)
    except Exception as e:
        log(f"Ошибка настроек сети: {e}. Работаю только локально.")
        return net

    net.port = cfg["port"]
    public = cfg["public"]
    if (cfg["upnp"] or cfg["firewall"]) and not public:
        log('UPnP/брандмауэр включены, значит нужен "public": true — включаю.')
        public = True
    net.host = "0.0.0.0" if public else "127.0.0.1"

    lan_ip = local_ip() if public else None
    external_ip = None
    external_port = cfg["external_port"] or cfg["port"]

    if cfg["firewall"]:
        try:
            open_firewall_port(cfg["port"])
        except Exception as e:
            log(f"Ошибка брандмауэра: {e}")

    if cfg["upnp"]:
        try:
            mapping = open_upnp_port(cfg["port"], external_port)
        except Exception as e:
            log(f"Ошибка UPnP: {e}")
            mapping = None
        if mapping:
            external_ip = mapping.external_ip
            log(f"Порт {external_port} открыт на роутере → {mapping.internal_ip}:{cfg['port']}")
            if external_ip:
                try:
                    if not ipaddress.ip_address(external_ip).is_global:
                        log(f"Внешний адрес роутера {external_ip} не публичный (провайдер использует NAT/CGNAT). "
                            "Из интернета панель открыться не сможет — спросите у провайдера «белый» IP.")
                except ValueError:
                    external_ip = None

    scheme = "http"
    if cfg["https"]:
        cert_path = base_dir / cfg["cert_file"]
        key_path = base_dir / cfg["key_file"]
        try:
            names = ["localhost", "127.0.0.1", socket.gethostname(), lan_ip or local_ip(), external_ip]
            if ensure_certificate(cert_path, key_path, names):
                net.ssl_context = make_ssl_context(cert_path, key_path)
                scheme = "https"
            else:
                log("HTTPS не включён, панель работает по обычному http.")
        except Exception as e:
            log(f"Ошибка HTTPS ({e}). Панель работает по обычному http.")

    log(f"Панель: {scheme}://127.0.0.1:{net.port}")
    if public:
        log(f"В локальной сети: {scheme}://{lan_ip}:{net.port}")
        if external_ip:
            net.public_url = f"{scheme}://{external_ip}:{external_port}"
            log(f"Из интернета: {net.public_url}")
    return net
