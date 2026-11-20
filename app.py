#!/usr/bin/env python3
"""IPP GUI - Web-based IPP printer management tool with GNOME Adwaita styling."""

import struct
import json
import io
import os
import datetime
import subprocess
import tempfile
import shutil
from flask import Flask, request, jsonify, send_from_directory

import requests as http_requests
from requests.exceptions import RequestException
import urllib3
import socket
import ipaddress
import threading
import concurrent.futures

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Try to import zeroconf for mDNS discovery
try:
    from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange
    HAS_ZEROCONF = True
except ImportError:
    HAS_ZEROCONF = False

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024 * 1024  # 256MB max upload

# ---------------------------------------------------------------------------
# IPP Protocol Constants
# ---------------------------------------------------------------------------

IPP_OPERATIONS = {
    'get-printer-attributes': 0x000B,
    'get-jobs': 0x000A,
    'get-job-attributes': 0x0009,
    'print-job': 0x0002,
    'validate-job': 0x0004,
    'cancel-job': 0x0008,
    'hold-job': 0x000C,
    'release-job': 0x000D,
    'restart-job': 0x000E,
    'pause-printer': 0x0010,
    'resume-printer': 0x0011,
    'purge-jobs': 0x0012,
    'cups-get-printers': 0x4002,
}

VALUE_TAGS = {
    0x10: 'unsupported',
    0x12: 'unknown',
    0x13: 'no-value',
    0x21: 'integer',
    0x22: 'boolean',
    0x23: 'enum',
    0x30: 'octetString',
    0x31: 'dateTime',
    0x32: 'resolution',
    0x33: 'rangeOfInteger',
    0x34: 'begCollection',
    0x35: 'textWithLanguage',
    0x36: 'nameWithLanguage',
    0x37: 'endCollection',
    0x41: 'textWithoutLanguage',
    0x42: 'nameWithoutLanguage',
    0x44: 'keyword',
    0x45: 'uri',
    0x46: 'uriScheme',
    0x47: 'charset',
    0x48: 'naturalLanguage',
    0x49: 'mimeMediaType',
    0x4A: 'memberAttrName',
}

DELIMITER_TAGS = {
    0x01: 'operation-attributes',
    0x02: 'job-attributes',
    0x03: 'end-of-attributes',
    0x04: 'printer-attributes',
    0x05: 'unsupported-attributes',
    0x06: 'subscription-attributes',
    0x07: 'event-notification-attributes',
    0x08: 'resource-attributes',
    0x09: 'document-attributes',
}

STATUS_CODES = {
    0x0000: 'successful-ok',
    0x0001: 'successful-ok-ignored-or-substituted-attributes',
    0x0002: 'successful-ok-conflicting-attributes',
    0x0003: 'successful-ok-ignored-subscriptions',
    0x0005: 'successful-ok-too-many-events',
    0x0400: 'client-error-bad-request',
    0x0401: 'client-error-forbidden',
    0x0402: 'client-error-not-authenticated',
    0x0403: 'client-error-not-authorized',
    0x0404: 'client-error-not-possible',
    0x0405: 'client-error-timeout',
    0x0406: 'client-error-not-found',
    0x0407: 'client-error-gone',
    0x0408: 'client-error-request-entity-too-large',
    0x0409: 'client-error-request-value-too-long',
    0x040A: 'client-error-document-format-not-supported',
    0x040B: 'client-error-attributes-or-values-not-supported',
    0x040C: 'client-error-uri-scheme-not-supported',
    0x040D: 'client-error-charset-not-supported',
    0x040E: 'client-error-conflicting-attributes',
    0x0500: 'server-error-internal-error',
    0x0501: 'server-error-operation-not-supported',
    0x0502: 'server-error-service-unavailable',
    0x0503: 'server-error-version-not-supported',
    0x0504: 'server-error-device-error',
    0x0505: 'server-error-temporary-error',
    0x0506: 'server-error-not-accepting-jobs',
    0x0507: 'server-error-busy',
    0x0508: 'server-error-job-canceled',
}

JOB_STATES = {
    3: 'pending',
    4: 'pending-held',
    5: 'processing',
    6: 'processing-stopped',
    7: 'canceled',
    8: 'aborted',
    9: 'completed',
}

PRINTER_STATES = {
    3: 'idle',
    4: 'processing',
    5: 'stopped',
}


# ---------------------------------------------------------------------------
# IPP Encoding
# ---------------------------------------------------------------------------

def encode_attribute(tag, name, value):
    """Encode a single IPP attribute."""
    name_bytes = name.encode('utf-8') if isinstance(name, str) else name
    if isinstance(value, str):
        val_bytes = value.encode('utf-8')
    elif isinstance(value, bool):
        val_bytes = struct.pack('>b', 1 if value else 0)
    elif isinstance(value, int):
        val_bytes = struct.pack('>i', value)
    elif isinstance(value, bytes):
        val_bytes = value
    else:
        val_bytes = str(value).encode('utf-8')

    result = struct.pack('>b', tag)
    result += struct.pack('>H', len(name_bytes))
    result += name_bytes
    result += struct.pack('>H', len(val_bytes))
    result += val_bytes
    return result


def encode_ipp_request(operation_id, attributes=None, version=(1, 1),
                       request_id=1, extra_groups=None, document_data=None):
    """Build a complete IPP request binary payload."""
    data = struct.pack('>bbHI', version[0], version[1], operation_id, request_id)

    # Operation attributes group
    data += b'\x01'  # operation-attributes-tag
    data += encode_attribute(0x47, 'attributes-charset', 'utf-8')
    data += encode_attribute(0x48, 'attributes-natural-language', 'en')

    if attributes:
        for tag, name, value in attributes:
            data += encode_attribute(tag, name, value)

    # Extra attribute groups (e.g., job-attributes)
    if extra_groups:
        for group_tag, group_attrs in extra_groups:
            data += struct.pack('>b', group_tag)
            for tag, name, value in group_attrs:
                data += encode_attribute(tag, name, value)

    data += b'\x03'  # end-of-attributes-tag

    if document_data:
        data += document_data

    return data


# ---------------------------------------------------------------------------
# IPP Decoding
# ---------------------------------------------------------------------------

def decode_ipp_response(data):
    """Parse an IPP response binary payload into a structured dict."""
    if len(data) < 8:
        return {'error': 'Response too short', 'raw_hex': data.hex()}

    offset = 0
    version_major, version_minor, status_code, request_id = struct.unpack_from('>bbHI', data, offset)
    offset += 8

    result = {
        'version': f'{version_major}.{version_minor}',
        'status-code': status_code,
        'status-message': STATUS_CODES.get(status_code, f'unknown-0x{status_code:04x}'),
        'request-id': request_id,
        'attribute-groups': [],
    }

    current_group = None
    current_attr_name = None

    while offset < len(data):
        tag = struct.unpack_from('>B', data, offset)[0]
        offset += 1

        # End of attributes
        if tag == 0x03:
            if current_group is not None:
                result['attribute-groups'].append(current_group)
            break

        # Delimiter tag -> new group
        if tag in DELIMITER_TAGS:
            if current_group is not None:
                result['attribute-groups'].append(current_group)
            current_group = {
                'group-tag': DELIMITER_TAGS.get(tag, f'0x{tag:02x}'),
                'attributes': {}
            }
            current_attr_name = None
            continue

        # Attribute
        if offset + 2 > len(data):
            break
        name_length = struct.unpack_from('>H', data, offset)[0]
        offset += 2

        if offset + name_length > len(data):
            break
        name = data[offset:offset + name_length].decode('utf-8', errors='replace') if name_length > 0 else ''
        offset += name_length

        if offset + 2 > len(data):
            break
        value_length = struct.unpack_from('>H', data, offset)[0]
        offset += 2

        if offset + value_length > len(data):
            break
        raw_value = data[offset:offset + value_length]
        offset += value_length

        # Decode value based on tag
        decoded_value = decode_value(tag, raw_value)

        if current_group is None:
            current_group = {'group-tag': 'unknown', 'attributes': {}}

        # If name is empty, this is an additional value for the previous attribute
        if name == '' and current_attr_name:
            attr_name = current_attr_name
        else:
            attr_name = name
            current_attr_name = name

        if attr_name in current_group['attributes']:
            existing = current_group['attributes'][attr_name]
            if isinstance(existing, list):
                existing.append(decoded_value)
            else:
                current_group['attributes'][attr_name] = [existing, decoded_value]
        else:
            current_group['attributes'][attr_name] = decoded_value

    return result


def decode_value(tag, raw):
    """Decode a raw IPP attribute value based on its tag."""
    try:
        if tag == 0x21:  # integer
            return struct.unpack('>i', raw)[0] if len(raw) == 4 else raw.hex()
        elif tag == 0x22:  # boolean
            return bool(raw[0]) if len(raw) >= 1 else False
        elif tag == 0x23:  # enum
            val = struct.unpack('>i', raw)[0] if len(raw) == 4 else int.from_bytes(raw, 'big')
            return val
        elif tag == 0x31:  # dateTime (11 bytes RFC 2579)
            if len(raw) == 11:
                y, mo, d, h, mi, s, ds, direction, utc_h, utc_m = struct.unpack('>HBBBBBBcBB', raw)
                return f'{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}.{ds}{direction.decode()}{utc_h:02d}:{utc_m:02d}'
            return raw.hex()
        elif tag == 0x32:  # resolution
            if len(raw) == 9:
                xres, yres, units = struct.unpack('>iiB', raw)
                unit_name = 'dpi' if units == 3 else 'dpcm' if units == 4 else f'units={units}'
                return f'{xres}x{yres} {unit_name}'
            return raw.hex()
        elif tag == 0x33:  # rangeOfInteger
            if len(raw) == 8:
                lo, hi = struct.unpack('>ii', raw)
                return f'{lo}-{hi}'
            return raw.hex()
        elif tag in (0x41, 0x42, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A):
            return raw.decode('utf-8', errors='replace')
        elif tag in (0x30,):  # octetString
            return raw.hex()
        elif tag in (0x10, 0x12, 0x13):  # unsupported/unknown/no-value
            return None
        else:
            # Try as text, fall back to hex
            try:
                return raw.decode('utf-8')
            except (UnicodeDecodeError, ValueError):
                return raw.hex()
    except Exception:
        return raw.hex() if raw else None


# ---------------------------------------------------------------------------
# URI Helpers
# ---------------------------------------------------------------------------

def ipp_uri_to_http(uri):
    """Convert ipp:// or ipps:// URI to http:// or https:// for HTTP POST."""
    if uri.startswith('ipp://'):
        return 'http://' + uri[6:]
    elif uri.startswith('ipps://'):
        return 'https://' + uri[7:]
    return uri


# ---------------------------------------------------------------------------
# IPP Request Executor
# ---------------------------------------------------------------------------

def send_ipp_request(printer_uri, operation, attributes=None, extra_groups=None,
                     document_data=None, version=(1, 1), timeout=30,
                     verify_ssl=True):
    """Send an IPP request and return parsed response."""
    if not printer_uri.lower().startswith(('ipp://', 'ipps://')):
        return {'error': 'Only ipp:// and ipps:// printer URIs are allowed'}
    http_url = ipp_uri_to_http(printer_uri)

    op_id = IPP_OPERATIONS.get(operation)
    if op_id is None:
        return {'error': f'Unknown operation: {operation}'}

    # Build base attributes - printer-uri is always needed
    base_attrs = [(0x45, 'printer-uri', printer_uri)]
    if attributes:
        base_attrs.extend(attributes)

    payload = encode_ipp_request(
        operation_id=op_id,
        attributes=base_attrs,
        version=version,
        extra_groups=extra_groups,
        document_data=document_data,
    )

    try:
        # Don't use proxy for local/internal printer connections
        resp = http_requests.post(
            http_url,
            data=payload,
            headers={'Content-Type': 'application/ipp'},
            timeout=timeout,
            verify=verify_ssl,
            proxies={'http': None, 'https': None},
        )
        return decode_ipp_response(resp.content)
    except RequestException as e:
        return {'error': str(e)}


# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------

# Every /api/* call must carry this header. Browsers cannot add a custom
# header to a cross-origin request without a CORS preflight, which this
# server never approves, so other websites cannot drive the API (CSRF).
API_HEADER = 'X-IPP-Debug'


# Host header allowlist: blocks DNS-rebinding attacks, where a malicious
# domain is re-pointed to this machine so the browser treats it as same-origin
# (which would let it send the API header above). Extra names/IPs used to
# reach the server can be added with IPP_GUI_ALLOWED_HOSTS (comma-separated).
ALLOWED_HOSTS = {'localhost', '127.0.0.1', '::1'} | {
    h.strip().lower()
    for h in os.environ.get('IPP_GUI_ALLOWED_HOSTS', '').split(',') if h.strip()
}


def _request_hostname():
    """Return the hostname part of the Host header, without port/brackets."""
    host = (request.host or '').lower()
    if host.startswith('['):                 # [::1]:5631
        return host[1:host.find(']')] if ']' in host else host
    return host.rsplit(':', 1)[0] if ':' in host else host


@app.before_request
def check_request_origin():
    if _request_hostname() not in ALLOWED_HOSTS:
        return jsonify({'error': 'Host not allowed (see IPP_GUI_ALLOWED_HOSTS)'}), 403
    if request.path.startswith('/api/') and request.headers.get(API_HEADER) != '1':
        return jsonify({'error': f'Missing {API_HEADER} header'}), 403


@app.route('/')
def index():
    return HTML_PAGE


@app.route('/api/ipp', methods=['POST'])
def api_ipp():
    """Generic IPP operation endpoint."""
    data = request.json
    printer_uri = data.get('printer_uri', '').strip()
    operation = data.get('operation', '')
    timeout = int(data.get('timeout', 30))
    verify_ssl = data.get('verify_ssl', True)
    version_str = data.get('version', '1.1')
    extra_attrs = data.get('attributes', [])
    extra_op_attrs = data.get('extra_groups', None)

    if not printer_uri:
        return jsonify({'error': 'Printer URI is required'}), 400
    if not operation:
        return jsonify({'error': 'Operation is required'}), 400

    version = tuple(int(x) for x in version_str.split('.'))

    # Build attribute list from extra_attrs
    attrs = []
    for a in extra_attrs:
        tag = int(a['tag'], 16) if isinstance(a['tag'], str) else int(a['tag'])
        attrs.append((tag, a['name'], a['value'] if not isinstance(a['value'], int) else a['value']))

    result = send_ipp_request(
        printer_uri, operation, attributes=attrs,
        version=version, timeout=timeout, verify_ssl=verify_ssl,
        extra_groups=extra_op_attrs,
    )

    # Enrich enum values for readability
    enrich_response(result)

    return jsonify(result)


@app.route('/api/print-job', methods=['POST'])
def api_print_job():
    """Print job with file upload. Auto-converts Office documents to PDF."""
    printer_uri = request.form.get('printer_uri', '').strip()
    timeout = int(request.form.get('timeout', 60))
    verify_ssl = request.form.get('verify_ssl', 'true') == 'true'
    version_str = request.form.get('version', '1.1')
    job_name = request.form.get('job_name', 'IPP-GUI-Job')
    copies = int(request.form.get('copies', 1))
    sides = request.form.get('sides', '')
    color_mode = request.form.get('color_mode', '')
    media = request.form.get('media', '')
    requesting_user = request.form.get('requesting_user', 'ipp-gui')
    document_format = request.form.get('document_format', '')

    if not printer_uri:
        return jsonify({'error': 'Printer URI is required'}), 400

    uploaded = request.files.get('document')
    if not uploaded:
        return jsonify({'error': 'No document file uploaded'}), 400

    document_data = uploaded.read()
    fname = uploaded.filename or ''
    fname_lower = fname.lower()
    version = tuple(int(x) for x in version_str.split('.'))

    # Extensions that need LibreOffice conversion to PDF
    CONVERTIBLE_EXTS = (
        '.docx', '.doc', '.odt', '.rtf',       # Word / text
        '.xlsx', '.xls', '.ods',                 # Spreadsheets
        '.pptx', '.ppt', '.odp',                 # Presentations
        '.html', '.htm',                          # HTML
        '.csv',                                   # CSV
    )

    converted = False
    if fname_lower.endswith(CONVERTIBLE_EXTS) and not document_format:
        # Convert to PDF via LibreOffice
        pdf_data, convert_error = convert_to_pdf(document_data, fname)
        if convert_error:
            return jsonify({'error': f'Conversion to PDF failed: {convert_error}'}), 500
        document_data = pdf_data
        document_format = 'application/pdf'
        converted = True

    attrs = [
        (0x42, 'requesting-user-name', requesting_user),
        (0x42, 'job-name', job_name),
    ]

    if document_format:
        attrs.append((0x49, 'document-format', document_format))
    else:
        # Try to guess from filename
        if fname_lower.endswith('.pdf'):
            attrs.append((0x49, 'document-format', 'application/pdf'))
        elif fname_lower.endswith(('.ps', '.eps')):
            attrs.append((0x49, 'document-format', 'application/postscript'))
        elif fname_lower.endswith(('.jpg', '.jpeg')):
            attrs.append((0x49, 'document-format', 'image/jpeg'))
        elif fname_lower.endswith('.png'):
            attrs.append((0x49, 'document-format', 'image/png'))
        elif fname_lower.endswith('.txt'):
            attrs.append((0x49, 'document-format', 'text/plain'))
        else:
            attrs.append((0x49, 'document-format', 'application/octet-stream'))

    # Job template attributes (group 0x02)
    job_attrs = []
    if copies > 1:
        job_attrs.append((0x21, 'copies', copies))
    if sides:
        job_attrs.append((0x44, 'sides', sides))
    if color_mode:
        job_attrs.append((0x44, 'print-color-mode', color_mode))
    if media:
        job_attrs.append((0x44, 'media', media))

    extra_groups = []
    if job_attrs:
        extra_groups.append((0x02, job_attrs))

    result = send_ipp_request(
        printer_uri, 'print-job', attributes=attrs,
        extra_groups=extra_groups if extra_groups else None,
        document_data=document_data,
        version=version, timeout=timeout, verify_ssl=verify_ssl,
    )

    enrich_response(result)
    if converted:
        result['_converted'] = f'{fname} → PDF (via LibreOffice)'
    return jsonify(result)


def convert_to_pdf(data: bytes, filename: str) -> tuple:
    """Convert a document to PDF using LibreOffice headless.

    Returns (pdf_bytes, error_string). On success error_string is None.
    """
    lo_bin = shutil.which('libreoffice') or shutil.which('soffice')
    if not lo_bin:
        return None, 'LibreOffice not found on this system'

    tmpdir = tempfile.mkdtemp(prefix='ipp-gui-convert-')
    try:
        # Write the source file under a fixed name: the uploaded filename is
        # client-controlled and must never be used as a path component.
        ext = os.path.splitext(os.path.basename(filename))[1].lower()
        filename = 'input' + ext
        src_path = os.path.join(tmpdir, filename)
        with open(src_path, 'wb') as f:
            f.write(data)

        # Run LibreOffice conversion
        result = subprocess.run(
            [lo_bin, '--headless', '--norestore', '--convert-to', 'pdf',
             '--outdir', tmpdir, src_path],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'HOME': tmpdir},  # isolate LO profile
        )

        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            return None, f'LibreOffice exited with code {result.returncode}: {stderr}'

        # Find the generated PDF
        base = os.path.splitext(filename)[0]
        pdf_path = os.path.join(tmpdir, base + '.pdf')
        if not os.path.exists(pdf_path):
            # Sometimes LO changes the name slightly, search for any .pdf
            pdfs = [f for f in os.listdir(tmpdir) if f.lower().endswith('.pdf')]
            if pdfs:
                pdf_path = os.path.join(tmpdir, pdfs[0])
            else:
                return None, 'LibreOffice ran but no PDF was produced'

        with open(pdf_path, 'rb') as f:
            pdf_data = f.read()

        if len(pdf_data) == 0:
            return None, 'LibreOffice produced an empty PDF'

        return pdf_data, None

    except subprocess.TimeoutExpired:
        return None, 'LibreOffice conversion timed out (60s)'
    except Exception as e:
        return None, str(e)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
# ---------------------------------------------------------------------------

def discover_mdns(timeout_s=5):
    """Discover IPP printers via mDNS/DNS-SD (Avahi/Bonjour)."""
    if not HAS_ZEROCONF:
        return []

    found = []
    lock = threading.Lock()

    class Listener:
        def __init__(self, zc):
            self.zc = zc

        def add_service(self, zc, stype, name):
            info = zc.get_service_info(stype, name)
            if info is None:
                return
            addresses = [socket.inet_ntoa(addr) for addr in info.addresses if len(addr) == 4]
            if not addresses:
                return
            host = addresses[0]
            port = info.port
            props = {}
            if info.properties:
                for k, v in info.properties.items():
                    key = k.decode('utf-8', errors='replace') if isinstance(k, bytes) else str(k)
                    val = v.decode('utf-8', errors='replace') if isinstance(v, bytes) else str(v)
                    props[key] = val

            rp = props.get('rp', 'ipp/print')
            scheme = 'ipps' if '_ipps' in stype else 'ipp'
            uri = f"{scheme}://{host}:{port}/{rp}"
            display_name = props.get('ty', name.split('._')[0])
            product = props.get('product', '')
            if product.startswith('(') and product.endswith(')'):
                product = product[1:-1]

            with lock:
                found.append({
                    'uri': uri,
                    'name': display_name,
                    'host': host,
                    'port': port,
                    'product': product,
                    'method': 'mDNS/DNS-SD',
                    'properties': props,
                })

        def remove_service(self, zc, stype, name):
            pass

        def update_service(self, zc, stype, name):
            pass

    zc = Zeroconf()
    try:
        listener = Listener(zc)
        browsers = []
        for stype in ['_ipp._tcp.local.', '_ipps._tcp.local.']:
            browsers.append(ServiceBrowser(zc, stype, listener))
        import time
        time.sleep(timeout_s)
    finally:
        zc.close()

    # Deduplicate by URI
    seen = set()
    unique = []
    for p in found:
        if p['uri'] not in seen:
            seen.add(p['uri'])
            unique.append(p)
    return unique


def discover_port_scan(timeout_s=10, port=631):
    """Scan local subnet for hosts with IPP port open."""
    found = []
    lock = threading.Lock()

    # Get local IP and subnet
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        return []

    # Determine subnet (assume /24 for speed, unless we can detect)
    parts = local_ip.split('.')
    subnet = f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"

    try:
        network = ipaddress.IPv4Network(subnet, strict=False)
    except ValueError:
        return []

    def check_host(ip_str):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            result = sock.connect_ex((ip_str, port))
            sock.close()
            if result == 0:
                # Try to get hostname
                try:
                    hostname = socket.gethostbyaddr(ip_str)[0]
                except Exception:
                    hostname = ip_str
                uri = f"ipp://{ip_str}:{port}/ipp/print"
                with lock:
                    found.append({
                        'uri': uri,
                        'name': hostname,
                        'host': ip_str,
                        'port': port,
                        'product': '',
                        'method': 'Port scan',
                        'properties': {},
                    })
        except Exception:
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
        hosts = [str(ip) for ip in network.hosts() if str(ip) != local_ip]
        executor.map(check_host, hosts)

    return found


def discover_ipp_query(printers):
    """Query discovered printers via IPP to get real names and info."""
    enriched = []
    for p in printers:
        try:
            result = send_ipp_request(
                p['uri'], 'get-printer-attributes',
                attributes=[(0x44, 'requested-attributes', 'printer-name'),
                            (0x44, 'requested-attributes', 'printer-info'),
                            (0x44, 'requested-attributes', 'printer-make-and-model'),
                            (0x44, 'requested-attributes', 'printer-state'),
                            (0x44, 'requested-attributes', 'printer-uri-supported')],
                timeout=3, verify_ssl=False,
            )
            if 'attribute-groups' in result:
                for grp in result['attribute-groups']:
                    attrs = grp.get('attributes', {})
                    if attrs.get('printer-name'):
                        p['name'] = attrs['printer-name']
                    if attrs.get('printer-make-and-model'):
                        p['product'] = attrs['printer-make-and-model']
                    if attrs.get('printer-info'):
                        p['info'] = attrs['printer-info']
                    if attrs.get('printer-state'):
                        st = attrs['printer-state']
                        p['state'] = PRINTER_STATES.get(st, str(st)) if isinstance(st, int) else str(st)
                    if attrs.get('printer-uri-supported'):
                        supported = attrs['printer-uri-supported']
                        if isinstance(supported, str):
                            p['uri'] = supported
                        elif isinstance(supported, list) and supported:
                            p['uri'] = supported[0]
                p['ipp_query'] = True
        except Exception:
            p['ipp_query'] = False
        enriched.append(p)
    return enriched


@app.route('/api/discover', methods=['POST'])
def api_discover():
    """Discover IPP printers on the network."""
    data = request.json or {}
    methods = data.get('methods', ['mdns', 'scan'])
    scan_timeout = int(data.get('scan_timeout', 5))
    query_printers = data.get('query_printers', True)

    all_printers = []
    errors = []

    if 'mdns' in methods:
        if HAS_ZEROCONF:
            try:
                mdns_printers = discover_mdns(timeout_s=min(scan_timeout, 10))
                all_printers.extend(mdns_printers)
            except Exception as e:
                errors.append(f"mDNS error: {e}")
        else:
            errors.append("zeroconf not installed — mDNS discovery unavailable")

    if 'scan' in methods:
        try:
            scan_printers = discover_port_scan(timeout_s=scan_timeout)
            all_printers.extend(scan_printers)
        except Exception as e:
            errors.append(f"Port scan error: {e}")

    # Deduplicate by host
    seen_hosts = set()
    unique = []
    for p in all_printers:
        if p['host'] not in seen_hosts:
            seen_hosts.add(p['host'])
            unique.append(p)

    # Optionally query each printer via IPP for more details
    if query_printers and unique:
        unique = discover_ipp_query(unique)

    return jsonify({
        'printers': unique,
        'count': len(unique),
        'methods_used': methods,
        'has_zeroconf': HAS_ZEROCONF,
        'errors': errors,
    })


def enrich_response(result):
    """Add human-readable labels to enum values."""
    if not isinstance(result, dict) or 'attribute-groups' not in result:
        return
    for group in result.get('attribute-groups', []):
        attrs = group.get('attributes', {})
        # Job state
        if 'job-state' in attrs:
            val = attrs['job-state']
            if isinstance(val, int):
                attrs['job-state'] = f'{JOB_STATES.get(val, "unknown")} ({val})'
        # Printer state
        if 'printer-state' in attrs:
            val = attrs['printer-state']
            if isinstance(val, int):
                attrs['printer-state'] = f'{PRINTER_STATES.get(val, "unknown")} ({val})'


# ---------------------------------------------------------------------------
# HTML / CSS / JS (embedded single-page app)
# ---------------------------------------------------------------------------

HTML_PAGE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>IPP GUI</title>
<style>
/* ---- Adwaita-inspired CSS variables ---- */
:root {
  --bg-primary: #fafafa;
  --bg-secondary: #ffffff;
  --bg-tertiary: #f0f0f0;
  --bg-sidebar: #f6f5f4;
  --bg-card: #ffffff;
  --bg-hover: #e8e8e8;
  --bg-active: #d4d4d4;
  --bg-input: #ffffff;
  --bg-header: #e8e6e4;
  --text-primary: #2e3436;
  --text-secondary: #5e5c5b;
  --text-muted: #929190;
  --border-color: #d5d3d1;
  --accent: #1c71d8;
  --accent-hover: #1a5fb4;
  --accent-bg: #d4e4f7;
  --success: #26a269;
  --success-bg: #d4edda;
  --error: #c01c28;
  --error-bg: #f8d7da;
  --warning: #e5a50a;
  --warning-bg: #fff3cd;
  --shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.06);
  --shadow-lg: 0 4px 12px rgba(0,0,0,0.1);
  --radius: 12px;
  --radius-sm: 8px;
  --radius-xs: 6px;
  --font: system-ui, 'Cantarell', -apple-system, 'Segoe UI', sans-serif;
  --mono: 'JetBrains Mono', 'Cascadia Code', 'Fira Code', 'Source Code Pro', monospace;
  --transition: 150ms ease;
}

[data-theme="dark"] {
  --bg-primary: #1e1e1e;
  --bg-secondary: #2d2d2d;
  --bg-tertiary: #353535;
  --bg-sidebar: #252525;
  --bg-card: #2d2d2d;
  --bg-hover: #3d3d3d;
  --bg-active: #4d4d4d;
  --bg-input: #353535;
  --bg-header: #303030;
  --text-primary: #f0f0f0;
  --text-secondary: #b0b0b0;
  --text-muted: #808080;
  --border-color: #454545;
  --accent: #62a0ea;
  --accent-hover: #4a90d9;
  --accent-bg: #1a3a5c;
  --success: #57e389;
  --success-bg: #1a3a2a;
  --error: #ff7b7b;
  --error-bg: #3a1a1a;
  --warning: #f8e45c;
  --warning-bg: #3a3a1a;
  --shadow: 0 1px 3px rgba(0,0,0,0.3);
  --shadow-lg: 0 4px 12px rgba(0,0,0,0.4);
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: var(--font);
  background: var(--bg-primary);
  color: var(--text-primary);
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ---- Header bar ---- */
.header-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--bg-header);
  border-bottom: 1px solid var(--border-color);
  padding: 6px 16px;
  height: 48px;
  flex-shrink: 0;
  -webkit-app-region: drag;
}

.header-bar h1 {
  font-size: 15px;
  font-weight: 700;
  display: flex;
  align-items: center;
  gap: 8px;
}

.header-bar h1 svg { width: 22px; height: 22px; }

.header-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

/* ---- Buttons ---- */
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-xs);
  background: var(--bg-card);
  color: var(--text-primary);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all var(--transition);
  white-space: nowrap;
}

.btn:hover { background: var(--bg-hover); }
.btn:active { background: var(--bg-active); }

.btn-accent {
  background: var(--accent);
  color: #fff;
  border-color: transparent;
}
.btn-accent:hover { background: var(--accent-hover); }
.btn-accent:active { opacity: 0.85; }

.btn-flat {
  background: transparent;
  border-color: transparent;
  padding: 6px 8px;
}
.btn-flat:hover { background: var(--bg-hover); }

.btn-sm { padding: 4px 10px; font-size: 12px; }

.btn-danger {
  background: var(--error);
  color: #fff;
  border-color: transparent;
}
.btn-danger:hover { opacity: 0.85; }

.icon-btn {
  width: 34px;
  height: 34px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--text-primary);
  border-radius: var(--radius-xs);
  cursor: pointer;
  transition: all var(--transition);
}
.icon-btn:hover { background: var(--bg-hover); }
.icon-btn svg { width: 18px; height: 18px; }

/* ---- Main layout ---- */
.main-layout {
  display: flex;
  flex: 1;
  overflow: hidden;
}

/* ---- Sidebar ---- */
.sidebar {
  width: 340px;
  min-width: 300px;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border-color);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}

.sidebar-section {
  padding: 16px;
  border-bottom: 1px solid var(--border-color);
}

.sidebar-section:last-child { border-bottom: none; }

.section-title {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  margin-bottom: 10px;
}

/* ---- Form controls ---- */
.form-group {
  margin-bottom: 10px;
}

.form-group label {
  display: block;
  font-size: 13px;
  font-weight: 500;
  margin-bottom: 4px;
  color: var(--text-secondary);
}

.form-control {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-xs);
  background: var(--bg-input);
  color: var(--text-primary);
  font: inherit;
  font-size: 13px;
  transition: border-color var(--transition), box-shadow var(--transition);
}

.form-control:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-bg);
}

select.form-control {
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath d='M6 8L1 3h10z' fill='%23888'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 8px center;
  padding-right: 28px;
}

textarea.form-control { resize: vertical; min-height: 60px; }

.form-row {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}

.form-row .form-group { flex: 1; }

.checkbox-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
}

.checkbox-row input[type="checkbox"] {
  width: 18px;
  height: 18px;
  accent-color: var(--accent);
}

.checkbox-row label {
  font-size: 13px;
  color: var(--text-secondary);
  margin: 0;
  cursor: pointer;
}

/* ---- Recent URIs ---- */
.recent-list {
  list-style: none;
  margin-top: 6px;
}

.recent-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border-radius: var(--radius-xs);
  cursor: pointer;
  font-size: 12px;
  color: var(--text-secondary);
  transition: background var(--transition);
  word-break: break-all;
}

.recent-item:hover { background: var(--bg-hover); }

.recent-item .remove-recent {
  margin-left: auto;
  opacity: 0;
  flex-shrink: 0;
  transition: opacity var(--transition);
}

.recent-item:hover .remove-recent { opacity: 1; }

/* ---- Content area ---- */
.content-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-primary);
}

/* ---- Status bar ---- */
.status-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
  background: var(--bg-card);
  border-bottom: 1px solid var(--border-color);
  min-height: 44px;
}

.status-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 100px;
  font-size: 12px;
  font-weight: 600;
}

.status-success { background: var(--success-bg); color: var(--success); }
.status-error { background: var(--error-bg); color: var(--error); }
.status-warning { background: var(--warning-bg); color: var(--warning); }
.status-idle { background: var(--bg-tertiary); color: var(--text-muted); }

.status-info {
  font-size: 12px;
  color: var(--text-muted);
}

/* ---- Tabs ---- */
.tab-bar {
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--border-color);
  background: var(--bg-card);
  padding: 0 16px;
}

.tab-btn {
  padding: 10px 18px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: all var(--transition);
}

.tab-btn:hover { color: var(--text-primary); background: var(--bg-hover); }

.tab-btn.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

.tab-content {
  flex: 1;
  overflow: auto;
  padding: 16px;
}

.tab-panel { display: none; }
.tab-panel.active { display: block; }

/* ---- Tree view ---- */
.tree-group {
  margin-bottom: 8px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--bg-card);
}

.tree-group-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  background: var(--bg-tertiary);
  cursor: pointer;
  font-weight: 600;
  font-size: 13px;
  user-select: none;
  transition: background var(--transition);
}

.tree-group-header:hover { background: var(--bg-hover); }

.tree-group-header .arrow {
  width: 16px;
  height: 16px;
  transition: transform var(--transition);
  flex-shrink: 0;
}

.tree-group-header.collapsed .arrow { transform: rotate(-90deg); }

.tree-group-body { padding: 0; }

.tree-group-header.collapsed + .tree-group-body { display: none; }

.tree-attr {
  display: flex;
  padding: 6px 14px 6px 38px;
  border-top: 1px solid var(--border-color);
  font-size: 13px;
  line-height: 1.5;
  gap: 12px;
}

.tree-attr:first-child { border-top: none; }

.attr-name {
  font-weight: 500;
  color: var(--accent);
  min-width: 220px;
  flex-shrink: 0;
  word-break: break-word;
  font-family: var(--mono);
  font-size: 12px;
}

.attr-value {
  color: var(--text-primary);
  word-break: break-word;
  flex: 1;
  font-family: var(--mono);
  font-size: 12px;
}

.attr-value .multi-val {
  display: block;
  padding: 1px 0;
}

/* ---- JSON view ---- */
.json-view {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  padding: 16px;
  overflow: auto;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

/* ---- Loading spinner ---- */
.spinner {
  display: inline-block;
  width: 18px;
  height: 18px;
  border: 2.5px solid var(--border-color);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

/* ---- Empty state ---- */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-muted);
  gap: 12px;
  padding: 40px;
  text-align: center;
}

.empty-state svg { width: 64px; height: 64px; opacity: 0.3; }
.empty-state p { font-size: 14px; }

/* ---- Print job form ---- */
.print-form-panel {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  padding: 20px;
  max-width: 600px;
}

.print-form-panel h3 {
  font-size: 15px;
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.file-drop {
  border: 2px dashed var(--border-color);
  border-radius: var(--radius-sm);
  padding: 32px 20px;
  text-align: center;
  cursor: pointer;
  transition: all var(--transition);
  color: var(--text-muted);
  font-size: 13px;
}

.file-drop:hover, .file-drop.drag-over {
  border-color: var(--accent);
  background: var(--accent-bg);
  color: var(--accent);
}

.file-drop input[type="file"] { display: none; }

.file-drop .file-name {
  margin-top: 8px;
  font-weight: 600;
  color: var(--text-primary);
}

/* ---- Operation cards ---- */
.op-card {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  cursor: pointer;
  transition: all var(--transition);
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 4px;
}

.op-card:hover { background: var(--bg-hover); }
.op-card.selected { border-color: var(--accent); background: var(--accent-bg); }

.op-card .op-icon {
  width: 32px;
  height: 32px;
  border-radius: var(--radius-xs);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  font-size: 16px;
}

.op-card .op-info { flex: 1; min-width: 0; }
.op-card .op-name { font-size: 13px; font-weight: 600; }
.op-card .op-desc { font-size: 11px; color: var(--text-muted); }

/* ---- Scrollbar ---- */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
  background: var(--border-color);
  border-radius: 4px;
}
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

/* ---- Job list ---- */
.job-list { }

.job-card {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  padding: 14px 16px;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 16px;
}

.job-card .job-info { flex: 1; }
.job-card .job-title { font-weight: 600; font-size: 13px; }
.job-card .job-meta { font-size: 12px; color: var(--text-muted); margin-top: 2px; }

.job-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}

/* ---- Utility ---- */
.hidden { display: none !important; }
.mb-8 { margin-bottom: 8px; }
.mb-16 { margin-bottom: 16px; }
.mt-8 { margin-top: 8px; }
.mt-16 { margin-top: 16px; }
.flex-row { display: flex; align-items: center; gap: 8px; }
.flex-1 { flex: 1; }
.text-center { text-align: center; }

/* ---- Toast ---- */
.toast-container {
  position: fixed;
  bottom: 20px;
  right: 20px;
  z-index: 1000;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.toast {
  padding: 10px 16px;
  border-radius: var(--radius-xs);
  font-size: 13px;
  font-weight: 500;
  box-shadow: var(--shadow-lg);
  animation: slideIn 0.3s ease;
  max-width: 400px;
}

.toast-success { background: var(--success); color: #fff; }
.toast-error { background: var(--error); color: #fff; }

@keyframes slideIn {
  from { transform: translateX(100%); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}
</style>
</head>
<body>

<!-- ============ HEADER ============ -->
<div class="header-bar">
  <h1>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="2" width="12" height="6" rx="1"/><rect x="2" y="8" width="20" height="10" rx="2"/><path d="M6 18v4h12v-4"/><circle cx="18" cy="12" r="1" fill="currentColor"/></svg>
    IPP GUI
  </h1>
  <div class="header-actions">
    <span id="statusText" class="status-info">Ready</span>
    <button class="icon-btn" id="themeToggle" title="Toggle theme">
      <svg id="themeIcon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>
    </button>
  </div>
</div>

<!-- ============ MAIN ============ -->
<div class="main-layout">

  <!-- ---- SIDEBAR ---- -->
  <div class="sidebar" id="sidebar">

    <!-- Connection -->
    <div class="sidebar-section">
      <div class="section-title">Connection</div>
      <div class="form-group">
        <label>Printer URI</label>
        <div style="display:flex; gap:6px;">
          <input type="text" class="form-control" id="printerUri" placeholder="ipp://printer.local:631/ipp/print" autocomplete="off" style="flex:1;">
          <button class="btn btn-secondary" onclick="openDiscovery()" title="Discover printers on the network" style="padding:6px 12px; white-space:nowrap;">🔍 Discover</button>
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>IPP Version</label>
          <select class="form-control" id="ippVersion">
            <option value="1.1" selected>1.1</option>
            <option value="2.0">2.0</option>
            <option value="2.1">2.1</option>
            <option value="2.2">2.2</option>
          </select>
        </div>
        <div class="form-group">
          <label>Timeout (s)</label>
          <input type="number" class="form-control" id="timeout" value="30" min="1" max="300">
        </div>
      </div>
      <div class="checkbox-row">
        <input type="checkbox" id="verifySsl" checked>
        <label for="verifySsl">Verify SSL</label>
      </div>
    </div>

    <!-- Operations -->
    <div class="sidebar-section" style="flex:1; overflow-y:auto;">
      <div class="section-title">Operations</div>

      <div class="op-card selected" data-op="get-printer-attributes" onclick="selectOp(this)">
        <div class="op-icon">🖨️</div>
        <div class="op-info"><div class="op-name">Get Printer Attributes</div><div class="op-desc">Query printer information</div></div>
      </div>
      <div class="op-card" data-op="get-jobs" onclick="selectOp(this)">
        <div class="op-icon">📋</div>
        <div class="op-info"><div class="op-name">Get Jobs</div><div class="op-desc">List print jobs</div></div>
      </div>
      <div class="op-card" data-op="get-job-attributes" onclick="selectOp(this)">
        <div class="op-icon">🔍</div>
        <div class="op-info"><div class="op-name">Get Job Attributes</div><div class="op-desc">Get job details by ID</div></div>
      </div>
      <div class="op-card" data-op="print-job" onclick="selectOp(this)">
        <div class="op-icon">📄</div>
        <div class="op-info"><div class="op-name">Print Job</div><div class="op-desc">Submit a print job with file</div></div>
      </div>
      <div class="op-card" data-op="validate-job" onclick="selectOp(this)">
        <div class="op-icon">✅</div>
        <div class="op-info"><div class="op-name">Validate Job</div><div class="op-desc">Validate job attributes</div></div>
      </div>
      <div class="op-card" data-op="cancel-job" onclick="selectOp(this)">
        <div class="op-icon">🚫</div>
        <div class="op-info"><div class="op-name">Cancel Job</div><div class="op-desc">Cancel a print job</div></div>
      </div>
      <div class="op-card" data-op="hold-job" onclick="selectOp(this)">
        <div class="op-icon">⏸️</div>
        <div class="op-info"><div class="op-name">Hold Job</div><div class="op-desc">Hold a job in queue</div></div>
      </div>
      <div class="op-card" data-op="release-job" onclick="selectOp(this)">
        <div class="op-icon">▶️</div>
        <div class="op-info"><div class="op-name">Release Job</div><div class="op-desc">Release a held job</div></div>
      </div>
      <div class="op-card" data-op="restart-job" onclick="selectOp(this)">
        <div class="op-icon">🔄</div>
        <div class="op-info"><div class="op-name">Restart Job</div><div class="op-desc">Restart a completed job</div></div>
      </div>
      <div class="op-card" data-op="pause-printer" onclick="selectOp(this)">
        <div class="op-icon">⏯️</div>
        <div class="op-info"><div class="op-name">Pause Printer</div><div class="op-desc">Pause the printer</div></div>
      </div>
      <div class="op-card" data-op="resume-printer" onclick="selectOp(this)">
        <div class="op-icon">🔛</div>
        <div class="op-info"><div class="op-name">Resume Printer</div><div class="op-desc">Resume the printer</div></div>
      </div>
      <div class="op-card" data-op="purge-jobs" onclick="selectOp(this)">
        <div class="op-icon">🗑️</div>
        <div class="op-info"><div class="op-name">Purge Jobs</div><div class="op-desc">Purge all jobs</div></div>
      </div>
      <div class="op-card" data-op="cups-get-printers" onclick="selectOp(this)">
        <div class="op-icon">☕</div>
        <div class="op-info"><div class="op-name">CUPS Get Printers</div><div class="op-desc">List CUPS printers</div></div>
      </div>
    </div>

    <!-- Recent URIs -->
    <div class="sidebar-section" id="recentSection" style="max-height: 180px; overflow-y: auto;">
      <div class="section-title">Recent URIs</div>
      <ul class="recent-list" id="recentList"></ul>
    </div>
  </div>

  <!-- ---- CONTENT ---- -->
  <div class="content-area">

    <!-- Status bar -->
    <div class="status-bar" id="statusBar">
      <span id="statusBadge" class="status-badge status-idle">Idle</span>
      <span id="statusDetail" class="status-info"></span>
      <div style="flex:1"></div>
      <button class="btn btn-accent" id="executeBtn" onclick="executeOperation()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
        Execute
      </button>
    </div>

    <!-- Dynamic options area (for job ID, print options, etc.) -->
    <div id="optionsBar" class="hidden" style="padding: 10px 16px; background: var(--bg-card); border-bottom: 1px solid var(--border-color);">
    </div>

    <!-- Tabs -->
    <div class="tab-bar">
      <button class="tab-btn active" data-tab="tree" onclick="switchTab(this)">Tree View</button>
      <button class="tab-btn" data-tab="json" onclick="switchTab(this)">JSON</button>
      <button class="tab-btn" data-tab="print" onclick="switchTab(this)">Print Job</button>
    </div>

    <!-- Tab panels -->
    <div class="tab-content">
      <div class="tab-panel active" id="panel-tree">
        <div class="empty-state" id="emptyState">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="6" y="2" width="12" height="6" rx="1"/><rect x="2" y="8" width="20" height="10" rx="2"/><path d="M6 18v4h12v-4"/></svg>
          <p>Select an operation and click <strong>Execute</strong> to send an IPP request.</p>
        </div>
        <div id="treeView"></div>
      </div>
      <div class="tab-panel" id="panel-json">
        <div class="json-view" id="jsonView">No response yet.</div>
      </div>
      <div class="tab-panel" id="panel-print">
        <div class="print-form-panel">
          <h3>📄 Print Job</h3>
          <div class="form-group">
            <label>Document</label>
            <div class="file-drop" id="fileDrop" onclick="document.getElementById('fileInput').click()">
              <input type="file" id="fileInput" onchange="handleFileSelect(event)" accept=".pdf,.ps,.eps,.jpg,.jpeg,.png,.txt,.docx,.doc,.odt,.rtf,.xlsx,.xls,.ods,.pptx,.ppt,.odp,.html,.htm,.csv">
              <div>📂 Click or drag a file here</div>
              <div style="font-size:11px; color:var(--text-muted); margin-top:4px;">PDF, images, text — DOCX/XLSX/PPTX/ODT auto-converted to PDF</div>
              <div class="file-name" id="fileName"></div>
            </div>
          </div>
          <div class="form-group">
            <label>Job Name</label>
            <input type="text" class="form-control" id="jobName" value="IPP-GUI-Job">
          </div>
          <div class="form-group">
            <label>Requesting User</label>
            <input type="text" class="form-control" id="requestingUser" value="ipp-gui">
          </div>
          <div class="form-row">
            <div class="form-group">
              <label>Copies</label>
              <input type="number" class="form-control" id="copies" value="1" min="1" max="999">
            </div>
            <div class="form-group">
              <label>Sides</label>
              <select class="form-control" id="sides">
                <option value="">(default)</option>
                <option value="one-sided">One-sided</option>
                <option value="two-sided-long-edge">Two-sided (long)</option>
                <option value="two-sided-short-edge">Two-sided (short)</option>
              </select>
            </div>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label>Color Mode</label>
              <select class="form-control" id="colorMode">
                <option value="">(default)</option>
                <option value="auto">Auto</option>
                <option value="color">Color</option>
                <option value="monochrome">Monochrome</option>
              </select>
            </div>
            <div class="form-group">
              <label>Media</label>
              <select class="form-control" id="media">
                <option value="">(default)</option>
                <option value="iso_a4_210x297mm">A4</option>
                <option value="na_letter_8.5x11in">Letter</option>
                <option value="na_legal_8.5x14in">Legal</option>
                <option value="iso_a3_297x420mm">A3</option>
                <option value="iso_a5_148x210mm">A5</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>Document Format</label>
            <select class="form-control" id="documentFormat">
              <option value="">(auto-detect)</option>
              <option value="application/pdf">PDF</option>
              <option value="application/postscript">PostScript</option>
              <option value="image/jpeg">JPEG</option>
              <option value="image/png">PNG</option>
              <option value="text/plain">Plain Text</option>
              <option value="application/octet-stream">Octet Stream</option>
            </select>
          </div>
          <button class="btn btn-accent mt-16" onclick="submitPrintJob()" style="width:100%">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="6" y="2" width="12" height="6" rx="1"/><rect x="2" y="8" width="20" height="10" rx="2"/><path d="M6 18v4h12v-4"/></svg>
            Print
          </button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Toast container -->
<div class="toast-container" id="toastContainer"></div>

<script>
// ============================================================
// State
// ============================================================
let currentOp = 'get-printer-attributes';
let lastResponse = null;
let selectedFile = null;

// ============================================================
// Theme
// ============================================================
function initTheme() {
  const saved = localStorage.getItem('ipp-gui-theme') || 'light';
  document.documentElement.setAttribute('data-theme', saved);
  updateThemeIcon(saved);
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'light';
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('ipp-gui-theme', next);
  updateThemeIcon(next);
}

function updateThemeIcon(theme) {
  const icon = document.getElementById('themeIcon');
  if (theme === 'dark') {
    icon.innerHTML = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>';
  } else {
    icon.innerHTML = '<circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>';
  }
}

document.getElementById('themeToggle').addEventListener('click', toggleTheme);
initTheme();

// ============================================================
// Operations
// ============================================================
const JOB_OPS = ['get-job-attributes', 'cancel-job', 'hold-job', 'release-job', 'restart-job'];
const JOBS_OPS = ['get-jobs'];

function selectOp(el) {
  document.querySelectorAll('.op-card').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  currentOp = el.dataset.op;
  updateOptionsBar();
}

function updateOptionsBar() {
  const bar = document.getElementById('optionsBar');
  bar.innerHTML = '';
  bar.classList.add('hidden');

  if (JOB_OPS.includes(currentOp)) {
    bar.classList.remove('hidden');
    bar.innerHTML = `
      <div class="flex-row">
        <label style="font-size:13px;font-weight:500;white-space:nowrap">Job ID:</label>
        <input type="number" class="form-control" id="jobId" placeholder="e.g. 42" min="1" style="max-width:120px">
        <label style="font-size:13px;font-weight:500;white-space:nowrap;margin-left:8px">User:</label>
        <input type="text" class="form-control" id="opUser" placeholder="requesting-user" value="ipp-gui" style="max-width:160px">
      </div>`;
  } else if (JOBS_OPS.includes(currentOp)) {
    bar.classList.remove('hidden');
    bar.innerHTML = `
      <div class="flex-row">
        <label style="font-size:13px;font-weight:500;white-space:nowrap">Which Jobs:</label>
        <select class="form-control" id="whichJobs" style="max-width:160px">
          <option value="not-completed">Not Completed</option>
          <option value="completed">Completed</option>
          <option value="all">All</option>
        </select>
        <label style="font-size:13px;font-weight:500;white-space:nowrap;margin-left:8px">Limit:</label>
        <input type="number" class="form-control" id="jobsLimit" value="50" min="1" max="9999" style="max-width:80px">
        <label style="font-size:13px;font-weight:500;white-space:nowrap;margin-left:8px">User:</label>
        <input type="text" class="form-control" id="opUser" placeholder="requesting-user" value="ipp-gui" style="max-width:160px">
      </div>`;
  } else if (currentOp === 'purge-jobs') {
    bar.classList.remove('hidden');
    bar.innerHTML = `
      <div class="flex-row">
        <label style="font-size:13px;font-weight:500;white-space:nowrap">User:</label>
        <input type="text" class="form-control" id="opUser" placeholder="requesting-user" value="ipp-gui" style="max-width:160px">
        <span style="font-size:12px;color:var(--error);margin-left:8px">⚠️ This will purge ALL jobs!</span>
      </div>`;
  } else if (currentOp === 'validate-job') {
    bar.classList.remove('hidden');
    bar.innerHTML = `
      <div class="flex-row">
        <label style="font-size:13px;font-weight:500;white-space:nowrap">Document Format:</label>
        <select class="form-control" id="valDocFormat" style="max-width:200px">
          <option value="application/pdf">PDF</option>
          <option value="application/postscript">PostScript</option>
          <option value="image/jpeg">JPEG</option>
          <option value="image/png">PNG</option>
          <option value="text/plain">Plain Text</option>
          <option value="application/octet-stream">Octet Stream</option>
        </select>
        <label style="font-size:13px;font-weight:500;white-space:nowrap;margin-left:8px">User:</label>
        <input type="text" class="form-control" id="opUser" placeholder="requesting-user" value="ipp-gui" style="max-width:160px">
      </div>`;
  }
}

// ============================================================
// Execute
// ============================================================
async function executeOperation() {
  if (currentOp === 'print-job') {
    // Switch to print tab
    document.querySelector('[data-tab="print"]').click();
    return;
  }

  const uri = document.getElementById('printerUri').value.trim();
  if (!uri) { showToast('Enter a printer URI', 'error'); return; }

  const version = document.getElementById('ippVersion').value;
  const timeout = document.getElementById('timeout').value;
  const verifySsl = document.getElementById('verifySsl').checked;

  // Build attributes based on operation
  const attrs = [];
  const userEl = document.getElementById('opUser');
  const user = userEl ? userEl.value.trim() : 'ipp-gui';

  if (user) {
    attrs.push({ tag: '0x42', name: 'requesting-user-name', value: user });
  }

  if (JOB_OPS.includes(currentOp)) {
    const jobId = document.getElementById('jobId')?.value;
    if (!jobId) { showToast('Enter a Job ID', 'error'); return; }
    attrs.push({ tag: '0x21', name: 'job-id', value: parseInt(jobId) });
  }

  if (JOBS_OPS.includes(currentOp)) {
    const whichJobs = document.getElementById('whichJobs')?.value || 'not-completed';
    const limit = document.getElementById('jobsLimit')?.value || '50';
    attrs.push({ tag: '0x44', name: 'which-jobs', value: whichJobs });
    attrs.push({ tag: '0x21', name: 'limit', value: parseInt(limit) });
  }

  if (currentOp === 'validate-job') {
    const fmt = document.getElementById('valDocFormat')?.value || 'application/pdf';
    attrs.push({ tag: '0x49', name: 'document-format', value: fmt });
    attrs.push({ tag: '0x42', name: 'job-name', value: 'validate-test' });
  }

  setLoading(true);
  saveRecentUri(uri);

  try {
    const resp = await fetch('/api/ipp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-IPP-Debug': '1' },
      body: JSON.stringify({
        printer_uri: uri,
        operation: currentOp,
        version: version,
        timeout: parseInt(timeout),
        verify_ssl: verifySsl,
        attributes: attrs,
      })
    });

    const data = await resp.json();
    lastResponse = data;
    displayResponse(data);
  } catch (err) {
    lastResponse = { error: err.message };
    displayResponse(lastResponse);
    showToast('Request failed: ' + err.message, 'error');
  } finally {
    setLoading(false);
  }
}

// ============================================================
// Print Job
// ============================================================
function handleFileSelect(e) {
  const file = e.target.files[0];
  if (file) {
    selectedFile = file;
    const convertible = /\.(docx?|odt|rtf|xlsx?|ods|pptx?|odp|html?|csv)$/i;
    const badge = convertible.test(file.name)
      ? ' <span style="background:var(--accent);color:#fff;padding:1px 8px;border-radius:10px;font-size:11px;">→ PDF</span>'
      : '';
    document.getElementById('fileName').innerHTML = file.name + ' (' + formatBytes(file.size) + ')' + badge;
  }
}

// Drag and drop
const fileDrop = document.getElementById('fileDrop');
fileDrop.addEventListener('dragover', e => { e.preventDefault(); fileDrop.classList.add('drag-over'); });
fileDrop.addEventListener('dragleave', () => fileDrop.classList.remove('drag-over'));
fileDrop.addEventListener('drop', e => {
  e.preventDefault();
  fileDrop.classList.remove('drag-over');
  if (e.dataTransfer.files.length) {
    selectedFile = e.dataTransfer.files[0];
    document.getElementById('fileInput').files = e.dataTransfer.files;
    document.getElementById('fileName').textContent = selectedFile.name + ' (' + formatBytes(selectedFile.size) + ')';
  }
});

async function submitPrintJob() {
  const uri = document.getElementById('printerUri').value.trim();
  if (!uri) { showToast('Enter a printer URI', 'error'); return; }
  if (!selectedFile) { showToast('Select a file to print', 'error'); return; }

  const formData = new FormData();
  formData.append('printer_uri', uri);
  formData.append('version', document.getElementById('ippVersion').value);
  formData.append('timeout', document.getElementById('timeout').value);
  formData.append('verify_ssl', document.getElementById('verifySsl').checked);
  formData.append('job_name', document.getElementById('jobName').value);
  formData.append('requesting_user', document.getElementById('requestingUser').value);
  formData.append('copies', document.getElementById('copies').value);
  formData.append('sides', document.getElementById('sides').value);
  formData.append('color_mode', document.getElementById('colorMode').value);
  formData.append('media', document.getElementById('media').value);
  formData.append('document_format', document.getElementById('documentFormat').value);
  formData.append('document', selectedFile);

  setLoading(true);
  saveRecentUri(uri);

  try {
    const resp = await fetch('/api/print-job', { method: 'POST', headers: { 'X-IPP-Debug': '1' }, body: formData });
    const data = await resp.json();
    lastResponse = data;
    displayResponse(data);
    // Switch to tree view
    document.querySelector('[data-tab="tree"]').click();
    if (!data.error && data['status-code'] !== undefined && data['status-code'] < 0x0400) {
      showToast('Print job submitted successfully!', 'success');
    }
  } catch (err) {
    lastResponse = { error: err.message };
    displayResponse(lastResponse);
    showToast('Print failed: ' + err.message, 'error');
  } finally {
    setLoading(false);
  }
}

// ============================================================
// Display Response
// ============================================================
function displayResponse(data) {
  // Update status
  if (data.error) {
    setStatus('error', 'Error', data.error);
  } else {
    const code = data['status-code'] || 0;
    const msg = data['status-message'] || 'unknown';
    if (code < 0x0400) {
      setStatus('success', `${msg}`, `Status: 0x${code.toString(16).padStart(4,'0')} | Version: ${data.version} | ReqID: ${data['request-id']}`);
    } else if (code < 0x0500) {
      setStatus('error', `${msg}`, `Client error 0x${code.toString(16).padStart(4,'0')}`);
    } else {
      setStatus('error', `${msg}`, `Server error 0x${code.toString(16).padStart(4,'0')}`);
    }
  }

  // JSON view
  document.getElementById('jsonView').textContent = JSON.stringify(data, null, 2);

  // Tree view
  const treeEl = document.getElementById('treeView');
  const emptyEl = document.getElementById('emptyState');

  if (data.error) {
    treeEl.innerHTML = `<div class="tree-group"><div class="tree-group-header"><span style="color:var(--error)">⚠️ Error</span></div><div class="tree-group-body"><div class="tree-attr"><span class="attr-name">message</span><span class="attr-value">${escapeHtml(data.error)}</span></div></div></div>`;
    emptyEl.classList.add('hidden');
    return;
  }

  if (!data['attribute-groups'] || data['attribute-groups'].length === 0) {
    treeEl.innerHTML = '<div style="padding:20px;color:var(--text-muted)">No attribute groups in response.</div>';
    emptyEl.classList.add('hidden');
    return;
  }

  emptyEl.classList.add('hidden');
  let html = '';
  let groupIdx = 0;

  for (const group of data['attribute-groups']) {
    const tag = group['group-tag'];
    const attrs = group.attributes || {};
    const attrCount = Object.keys(attrs).length;
    const groupLabel = tag.charAt(0).toUpperCase() + tag.slice(1).replace(/-/g, ' ');

    html += `<div class="tree-group">`;
    html += `<div class="tree-group-header" onclick="this.classList.toggle('collapsed')">`;
    html += `<svg class="arrow" viewBox="0 0 16 16" fill="currentColor"><path d="M4 6l4 4 4-4z"/></svg>`;
    html += `<span>${escapeHtml(groupLabel)}</span>`;
    html += `<span style="font-weight:400;font-size:11px;color:var(--text-muted);margin-left:auto">${attrCount} attributes</span>`;
    html += `</div>`;
    html += `<div class="tree-group-body">`;

    for (const [name, value] of Object.entries(attrs)) {
      html += `<div class="tree-attr">`;
      html += `<span class="attr-name">${escapeHtml(name)}</span>`;
      html += `<span class="attr-value">${formatValue(value)}</span>`;
      html += `</div>`;
    }

    html += `</div></div>`;
    groupIdx++;
  }

  treeEl.innerHTML = html;
}

function formatValue(val) {
  if (val === null || val === undefined) return '<span style="color:var(--text-muted)">∅</span>';
  if (Array.isArray(val)) {
    return val.map(v => `<span class="multi-val">${escapeHtml(String(v))}</span>`).join('');
  }
  if (typeof val === 'boolean') return val ? '✓ true' : '✗ false';
  return escapeHtml(String(val));
}

// ============================================================
// UI Helpers
// ============================================================
function setLoading(loading) {
  const btn = document.getElementById('executeBtn');
  if (loading) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Sending…';
    document.getElementById('statusText').textContent = 'Sending request…';
  } else {
    btn.disabled = false;
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg> Execute';
    document.getElementById('statusText').textContent = 'Ready';
  }
}

function setStatus(type, label, detail) {
  const badge = document.getElementById('statusBadge');
  badge.className = 'status-badge status-' + type;
  badge.textContent = label;
  document.getElementById('statusDetail').textContent = detail || '';
}

function switchTab(btn) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('panel-' + btn.dataset.tab).classList.add('active');
}

function showToast(msg, type = 'success') {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = 'toast toast-' + type;
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 300); }, 4000);
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

// ============================================================
// Recent URIs (localStorage)
// ============================================================
function loadRecentUris() {
  try { return JSON.parse(localStorage.getItem('ipp-gui-recent') || '[]'); } catch { return []; }
}

function saveRecentUri(uri) {
  let recent = loadRecentUris().filter(u => u !== uri);
  recent.unshift(uri);
  recent = recent.slice(0, 10);
  localStorage.setItem('ipp-gui-recent', JSON.stringify(recent));
  renderRecentUris();
}

function removeRecentUri(uri) {
  const recent = loadRecentUris().filter(u => u !== uri);
  localStorage.setItem('ipp-gui-recent', JSON.stringify(recent));
  renderRecentUris();
}

function renderRecentUris() {
  const list = document.getElementById('recentList');
  const recent = loadRecentUris();
  if (recent.length === 0) {
    document.getElementById('recentSection').classList.add('hidden');
    return;
  }
  document.getElementById('recentSection').classList.remove('hidden');
  list.innerHTML = recent.map(u => `
    <li class="recent-item" onclick="document.getElementById('printerUri').value='${escapeHtml(u)}'">
      🖨️ ${escapeHtml(u)}
      <button class="icon-btn remove-recent" onclick="event.stopPropagation();removeRecentUri('${escapeHtml(u)}')" title="Remove">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
      </button>
    </li>
  `).join('');
}

// ============================================================
// Keyboard shortcut
// ============================================================
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault();
    executeOperation();
  }
});

// ============================================================
// Init
// ============================================================
renderRecentUris();
updateOptionsBar();
</script>

<!-- Discovery Modal -->
<div id="discoveryModal" style="display:none; position:fixed; inset:0; z-index:1000; background:rgba(0,0,0,0.5); backdrop-filter:blur(4px); align-items:center; justify-content:center;">
  <div style="background:var(--bg-card); border-radius:16px; box-shadow:0 24px 80px rgba(0,0,0,0.3); width:700px; max-width:90vw; max-height:80vh; display:flex; flex-direction:column; border:1px solid var(--border);">
    <div style="display:flex; align-items:center; justify-content:space-between; padding:20px 24px 16px; border-bottom:1px solid var(--border);">
      <h2 style="margin:0; font-size:18px; font-weight:700; color:var(--text-primary);">🔍 Discover Printers</h2>
      <button onclick="closeDiscovery()" style="background:none; border:none; font-size:20px; cursor:pointer; color:var(--text-secondary); padding:4px 8px; border-radius:8px;" onmouseover="this.style.background='var(--bg-hover)'" onmouseout="this.style.background='none'">✕</button>
    </div>
    <div style="padding:16px 24px; border-bottom:1px solid var(--border);">
      <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
        <label style="display:flex; align-items:center; gap:4px; font-size:13px; color:var(--text-secondary); cursor:pointer;">
          <input type="checkbox" id="discMdns" checked> mDNS/DNS-SD (Bonjour)
        </label>
        <label style="display:flex; align-items:center; gap:4px; font-size:13px; color:var(--text-secondary); cursor:pointer;">
          <input type="checkbox" id="discScan" checked> Port scan (631)
        </label>
        <label style="display:flex; align-items:center; gap:4px; font-size:13px; color:var(--text-secondary);">
          Timeout:
          <input type="number" id="discTimeout" value="5" min="1" max="30" style="width:50px; padding:4px 6px; border-radius:6px; border:1px solid var(--border); background:var(--bg-input); color:var(--text-primary); font-size:13px;">s
        </label>
        <label style="display:flex; align-items:center; gap:4px; font-size:13px; color:var(--text-secondary); cursor:pointer;">
          <input type="checkbox" id="discQuery" checked> Query IPP details
        </label>
        <button class="btn btn-primary" id="discStartBtn" onclick="startDiscovery()" style="margin-left:auto; padding:6px 16px;">Scan</button>
      </div>
    </div>
    <div id="discoveryResults" style="flex:1; overflow-y:auto; padding:16px 24px; min-height:200px;">
      <div style="text-align:center; color:var(--text-muted); padding:40px 0; font-size:14px;">
        Click <b>Scan</b> to discover IPP printers on your local network.
      </div>
    </div>
  </div>
</div>

<script>
function openDiscovery() {
  const m = document.getElementById('discoveryModal');
  m.style.display = 'flex';
}
function closeDiscovery() {
  document.getElementById('discoveryModal').style.display = 'none';
}
document.getElementById('discoveryModal').addEventListener('click', function(e) {
  if (e.target === this) closeDiscovery();
});

async function startDiscovery() {
  const btn = document.getElementById('discStartBtn');
  const results = document.getElementById('discoveryResults');
  const methods = [];
  if (document.getElementById('discMdns').checked) methods.push('mdns');
  if (document.getElementById('discScan').checked) methods.push('scan');
  const timeout = parseInt(document.getElementById('discTimeout').value) || 5;
  const query = document.getElementById('discQuery').checked;

  btn.disabled = true;
  btn.textContent = 'Scanning…';
  results.innerHTML = '<div style="text-align:center; padding:40px 0;"><div class="spinner" style="margin:0 auto 12px;"></div><div style="color:var(--text-secondary); font-size:14px;">Scanning network… this may take up to ' + timeout + 's</div></div>';

  try {
    const resp = await fetch('/api/discover', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-IPP-Debug': '1'},
      body: JSON.stringify({methods, scan_timeout: timeout, query_printers: query})
    });
    const data = await resp.json();
    renderDiscoveryResults(data);
  } catch(e) {
    results.innerHTML = '<div style="color:var(--accent-error); padding:20px; text-align:center;">' + e.message + '</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Scan';
  }
}

function renderDiscoveryResults(data) {
  const results = document.getElementById('discoveryResults');
  let html = '';

  if (data.errors && data.errors.length > 0) {
    html += '<div style="background:var(--bg-tertiary); border-radius:8px; padding:8px 12px; margin-bottom:12px; font-size:12px; color:var(--text-secondary);">';
    data.errors.forEach(e => { html += '⚠️ ' + e + '<br>'; });
    html += '</div>';
  }

  if (!data.printers || data.printers.length === 0) {
    html += '<div style="text-align:center; color:var(--text-muted); padding:40px 0;">';
    html += '<div style="font-size:48px; margin-bottom:12px;">🖨️</div>';
    html += '<div style="font-size:14px; font-weight:600;">No printers found</div>';
    html += '<div style="font-size:12px; margin-top:4px;">Make sure your printers are on the same network and IPP is enabled.</div>';
    html += '</div>';
  } else {
    html += '<div style="font-size:12px; color:var(--text-muted); margin-bottom:10px;">' + data.count + ' printer(s) found</div>';
    data.printers.forEach((p, i) => {
      const stateColor = p.state === 'idle' ? 'var(--accent-success)' : p.state === 'processing' ? 'var(--accent-warning, #f5c211)' : 'var(--text-secondary)';
      const stateDot = p.state ? '<span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:' + stateColor + '; margin-right:6px;"></span>' + escapeHtml(String(p.state)) : '';
      html += '<div class="printer-disc-card" data-idx="' + i + '" style="border:1px solid var(--border); border-radius:12px; padding:14px 16px; margin-bottom:8px; cursor:pointer; transition:all .15s;" ';
      html += 'onmouseover="this.style.background=\'var(--bg-hover)\'; this.style.borderColor=\'var(--accent)\'" ';
      html += 'onmouseout="this.style.background=\'transparent\'; this.style.borderColor=\'var(--border)\'">';
      html += '<div style="display:flex; justify-content:space-between; align-items:start;">';
      html += '<div>';
      html += '<div style="font-weight:600; font-size:14px; color:var(--text-primary);">🖨️ ' + escapeHtml(p.name || 'Unknown Printer') + '</div>';
      if (p.product) html += '<div style="font-size:12px; color:var(--text-secondary); margin-top:2px;">' + escapeHtml(String(p.product)) + '</div>';
      html += '<div style="font-size:11px; color:var(--text-muted); margin-top:4px; font-family:monospace;">' + escapeHtml(String(p.uri)) + '</div>';
      html += '</div>';
      html += '<div style="text-align:right; flex-shrink:0;">';
      if (stateDot) html += '<div style="font-size:12px; display:flex; align-items:center;">' + stateDot + '</div>';
      html += '<div style="font-size:10px; color:var(--text-muted); margin-top:4px; background:var(--bg-tertiary); padding:2px 8px; border-radius:10px;">' + escapeHtml(String(p.method)) + '</div>';
      html += '</div>';
      html += '</div>';
      html += '</div>';
    });
  }

  results.innerHTML = html;
  // Attach click handlers with a closure instead of building inline JS from
  // network-supplied data (mDNS names/URIs are attacker-controllable).
  results.querySelectorAll('.printer-disc-card').forEach(card => {
    const p = data.printers[Number(card.dataset.idx)];
    card.addEventListener('click', () => selectDiscoveredPrinter(p.uri));
  });
}

function selectDiscoveredPrinter(uri) {
  document.getElementById('printerUri').value = uri;
  closeDiscovery();
  showToast('Printer selected: ' + uri, 'success');
  // Save to recent URIs
  saveUri(uri);
}
</script>
</body>
</html>
'''

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    host = os.environ.get('IPP_GUI_HOST', '127.0.0.1')
    port = int(os.environ.get('IPP_GUI_PORT', '5631'))
    print("=" * 50)
    print(f"  IPP GUI - http://localhost:{port}")
    print(f"  Allowed hosts: {', '.join(sorted(ALLOWED_HOSTS))}")
    print("=" * 50)
    if host not in ('127.0.0.1', 'localhost', '::1') and not os.environ.get('IPP_GUI_ALLOWED_HOSTS'):
        print("  Note: to reach the GUI from other machines, add the name/IP")
        print("  they use to IPP_GUI_ALLOWED_HOSTS (e.g. IPP_GUI_ALLOWED_HOSTS=192.0.2.5)")
    app.run(host=host, port=port, debug=False)
