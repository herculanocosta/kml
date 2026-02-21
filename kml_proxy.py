from flask import Flask, Response, request, send_from_directory
import requests, time, re, certifi, io, zipfile, hashlib, os
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

app = Flask(__name__)

# -------------------------
# Config
# -------------------------
SOURCE_KML_RECENT = "https://mapmil.igeoe.pt/localizador/kml/104BM80_30eff84?h=5"
SOURCE_KML_FULL   = "https://mapmil.igeoe.pt/localizador/kml/104BM80_30eff84"

ICON_SCALE = 1.7
LABEL_SCALE = 0.7

CACHE_SECONDS = 10
ALLOW_INSECURE_SSL = False

ALLOWED_ICON_PREFIX = "https://mapmil.igeoe.pt/localizador/kmlMarkers/"
ICON_CACHE_SECONDS = 3600

STALE_THRESHOLD_SECONDS = 3600
STALE_ICON_PATH = "/static/stale.png"          # served by this app
STALE_ICON_EMBED_PATH = "icons/stale.png"      # embedded in KMZ
STALE_ICON_SCALE = 0.8
STALE_LABEL_SCALE = 0.7
STALE_ICON_KML_COLOR = "ffffffff"              # ~25% opacity (AABBGGRR)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
STALE_ICON_FILE = os.path.join(STATIC_DIR, "stale.png")

KML_NS_URI = "http://www.opengis.net/kml/2.2"
KML_NS = {"kml": KML_NS_URI}

ET.register_namespace("", KML_NS_URI)
ET.register_namespace("gx", "http://www.google.com/kml/ext/2.2")
ET.register_namespace("atom", "http://www.w3.org/2005/Atom")

_cache = {"ts": 0.0, "kml": None}
_icon_cache = {}  # url -> (ts, bytes, content_type)

HREF_RE = r"<(?:\w+:)?href>\s*([^<]+)\s*</(?:\w+:)?href>"

# -------------------------
# Small XML helpers
# -------------------------
def _sub(parent: ET.Element, local: str) -> ET.Element:
    return ET.SubElement(parent, f"{{{KML_NS_URI}}}{local}")

def _find(parent: ET.Element, local: str) -> ET.Element | None:
    return parent.find(f"kml:{local}", namespaces=KML_NS)

def _findall(parent: ET.Element, path: str) -> list[ET.Element]:
    return parent.findall(path, namespaces=KML_NS)

def _ensure(parent: ET.Element, local: str) -> ET.Element:
    el = _find(parent, local)
    return el if el is not None else _sub(parent, local)

def _ensure_path(parent: ET.Element, locals_: list[str]) -> ET.Element:
    cur = parent
    for loc in locals_:
        nxt = _find(cur, loc)
        cur = nxt if nxt is not None else _sub(cur, loc)
    return cur

def mark_pm_stale(pm: ET.Element) -> None:
    ext = pm.find("kml:ExtendedData", namespaces=KML_NS)
    if ext is None:
        ext = _sub(pm, "ExtendedData")

    data = ET.SubElement(ext, f"{{{KML_NS_URI}}}Data", {"name": "stale"})
    val = ET.SubElement(data, f"{{{KML_NS_URI}}}value")
    val.text = "true"

def group_route_placemarks_into_folders(kml_xml: str) -> str:
    """
    Finds placemarks with id like "<base>_route" and groups them with "<base>"
    into a <Folder>. Leaves unmatched routes as-is.

    Works on the final KML string and returns a new KML string.
    """
    root = ET.fromstring(kml_xml)
    doc = root.find("kml:Document", KML_NS)
    if doc is None:
        return kml_xml

    # Only consider direct children of Document for stable / predictable re-ordering
    children = list(doc)

    # Index direct placemark children by id
    id_to_pm = {}
    for el in children:
        if el.tag == f"{{{KML_NS_URI}}}Placemark":
            pm_id = el.attrib.get("id")
            if pm_id:
                id_to_pm[pm_id] = el

    def is_route_id(pid: str) -> bool:
        return pid.endswith("_route")

    used = set()

    # We'll rebuild Document contents in a new list, then replace
    new_children = []
    for el in children:
        # Pass through non-placemark elements unchanged (name, etc.)
        if el.tag != f"{{{KML_NS_URI}}}Placemark":
            new_children.append(el)
            continue

        pm_id = el.attrib.get("id") or ""
        if not pm_id or pm_id in used:
            continue

        # If this is a base waypoint and there is a matching "<base>_route"
        route_id = f"{pm_id}_route"
        if route_id in id_to_pm:
            base_pm = el
            route_pm = id_to_pm[route_id]

            folder = ET.Element(f"{{{KML_NS_URI}}}Folder")
            # Folder name: use placemark name if present, else id
            base_name = base_pm.findtext("kml:name", default="", namespaces=KML_NS).strip()
            name_el = ET.SubElement(folder, f"{{{KML_NS_URI}}}name")
            name_el.text = base_name if base_name else pm_id

            folder.append(base_pm)
            folder.append(route_pm)

            new_children.append(folder)
            used.add(pm_id)
            used.add(route_id)
        else:
            # Not groupable: keep as-is
            new_children.append(el)
            used.add(pm_id)

    # Also include any route placemarks that were never added because their base wasn't present
    # (only if they were direct children and not already used)
    for el in children:
        if el.tag == f"{{{KML_NS_URI}}}Placemark":
            pid = el.attrib.get("id") or ""
            if pid and pid not in used and is_route_id(pid):
                new_children.append(el)
                used.add(pid)

    # Replace Document children
    doc.clear()
    for el in new_children:
        doc.append(el)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")

# -------------------------
# KML scale transform (string-based, keeps your behavior)
# -------------------------
def _replace_scale_in_block(block: str, tag_name: str, new_value: float) -> str:
    if f"<{tag_name}" not in block:
        return block
    m = re.search(rf"(<{tag_name}[^>]*>)([\s\S]*?)(</{tag_name}>)", block, flags=re.IGNORECASE)
    if not m:
        return block
    open_tag, inner, close_tag = m.group(1), m.group(2), m.group(3)

    if re.search(r"<scale>\s*[\s\S]*?\s*</scale>", inner, flags=re.IGNORECASE):
        inner = re.sub(
            r"(<scale>\s*)[\s\S]*?(\s*</scale>)",
            lambda mm: f"{mm.group(1)}{new_value}{mm.group(2)}",
            inner,
            flags=re.IGNORECASE
        )
    else:
        inner = f"<scale>{new_value}</scale>" + inner

    return block[:m.start()] + (open_tag + inner + close_tag) + block[m.end():]

def transform_kml_scales(kml: str) -> str:
    def patch_style(style_block: str) -> str:
        if "<IconStyle" not in style_block:
            style_block = style_block.replace("</Style>", f"<IconStyle><scale>{ICON_SCALE}</scale></IconStyle></Style>", 1)
        if "<LabelStyle" not in style_block:
            style_block = style_block.replace("</Style>", f"<LabelStyle><scale>{LABEL_SCALE}</scale></LabelStyle></Style>", 1)
        style_block = _replace_scale_in_block(style_block, "IconStyle", ICON_SCALE)
        style_block = _replace_scale_in_block(style_block, "LabelStyle", LABEL_SCALE)
        return style_block

    return re.sub(r"<Style[\s\S]*?</Style>", lambda m: patch_style(m.group(0)), kml, flags=re.IGNORECASE)

# -------------------------
# Fetch + parse GDH
# -------------------------
def fetch_kml(url: str) -> str:
    verify = False if ALLOW_INSECURE_SSL else certifi.where()
    r = requests.get(url, timeout=15, headers={"User-Agent": "KML-Proxy/ATAK-1.2"}, verify=verify)
    r.raise_for_status()
    return r.text

def extract_gdh_epoch_from_placemark(pm_xml: str) -> float:
    normalized = (pm_xml.replace("&lt;br&gt;", "<br>")
                        .replace("&lt;br/&gt;", "<br>")
                        .replace("&lt;br /&gt;", "<br>"))

    m = re.search(
        r"GDH\s+de\s+recep(?:ç|c)ão\s+do\s+último\s+sinal:?<br>\s*"
        r"([0-9]{2}[A-Z]{3}[0-9]{2})\s+"
        r"([0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.(\d+))?",
        normalized,
        flags=re.IGNORECASE
    )
    if not m:
        return 0.0

    date_part, time_part, ms_part = m.group(1), m.group(2), (m.group(3) or "0")
    day = int(date_part[0:2])
    mon_str = date_part[2:5].upper()
    yy = int(date_part[5:7])
    year = 2000 + yy if yy < 80 else 1900 + yy

    month_map = {
        "JAN": 1, "FEV": 2, "FEB": 2, "MAR": 3, "ABR": 4, "APR": 4,
        "MAI": 5, "MAY": 5, "JUN": 6, "JUL": 7, "AGO": 8, "AUG": 8,
        "SET": 9, "SEP": 9, "OUT": 10, "OCT": 10, "NOV": 11, "DEZ": 12, "DEC": 12
    }
    month = month_map.get(mon_str, 1)

    hh, mm, ss = map(int, time_part.split(":"))
    frac = float("0." + re.sub(r"\D", "", ms_part))

    import datetime
    dt = datetime.datetime(year, month, day, hh, mm, ss) + datetime.timedelta(seconds=frac)
    return dt.timestamp()

def placemark_fallback_key(pm_el: ET.Element) -> str:
    name = (pm_el.findtext("kml:name", default="", namespaces=KML_NS) or "").strip()
    coords = ""
    c_el = pm_el.find(".//kml:coordinates", namespaces=KML_NS)
    if c_el is not None and c_el.text:
        coords = c_el.text.strip()
    return hashlib.sha256(f"{name}|{coords}".encode("utf-8")).hexdigest()

# -------------------------
# Merge + dedupe
# -------------------------
def merge_kml_two_sources(kml_recent: str, kml_full: str) -> str:
    r_root = ET.fromstring(kml_recent)
    f_root = ET.fromstring(kml_full)

    r_doc = r_root.find("kml:Document", KML_NS)
    f_doc = f_root.find("kml:Document", KML_NS)

    out_root = ET.Element(f"{{{KML_NS_URI}}}kml")
    out_doc = _sub(out_root, "Document")
    _sub(out_doc, "name").text = "Merged KML"

    def collect(doc_el: ET.Element | None) -> list[ET.Element]:
        return [] if doc_el is None else doc_el.findall("kml:Placemark", KML_NS)

    chosen: dict[str, tuple[int, float, ET.Element]] = {}

    def consider(pm: ET.Element, pri: int):
        pm_id = pm.attrib.get("id")
        key = f"id:{pm_id}" if pm_id else f"fb:{placemark_fallback_key(pm)}"

        gdh = extract_gdh_epoch_from_placemark(ET.tostring(pm, encoding="unicode"))

        if key not in chosen:
            chosen[key] = (pri, gdh, pm); return

        prev_pri, prev_gdh, _ = chosen[key]

        # Prefer newer GDH; tie-break by source priority
        if gdh and prev_gdh:
            if gdh > prev_gdh or (gdh == prev_gdh and pri > prev_pri):
                chosen[key] = (pri, gdh, pm)
            return
        if gdh and not prev_gdh:
            chosen[key] = (pri, gdh, pm); return
        if prev_gdh and not gdh:
            return
        if pri > prev_pri:
            chosen[key] = (pri, gdh, pm)

    for pm in collect(f_doc): consider(pm, 1)
    for pm in collect(r_doc): consider(pm, 2)

    for k in sorted(chosen.keys()):
        out_doc.append(chosen[k][2])

    return ET.tostring(out_root, encoding="utf-8", xml_declaration=True).decode("utf-8")

def get_merged_kml_cached() -> str:
    now = time.time()
    if _cache["kml"] is not None and (now - _cache["ts"] < CACHE_SECONDS):
        return _cache["kml"]

    recent_scaled = transform_kml_scales(fetch_kml(SOURCE_KML_RECENT))
    full_scaled   = transform_kml_scales(fetch_kml(SOURCE_KML_FULL))

    merged = merge_kml_two_sources(recent_scaled, full_scaled)
    _cache.update({"kml": merged, "ts": now})
    return merged

# -------------------------
# Stale flagging (single path)
# -------------------------
def apply_stale_style(pm: ET.Element, stale_href: str) -> None:
    # Prefix name
    name_el = pm.find("kml:name", namespaces=KML_NS)
    if name_el is None:
        name_el = _sub(pm, "name"); name_el.text = "⚠"
    else:
        t = (name_el.text or "").strip()
        if not t.startswith("⚠"):
            name_el.text = f"⚠ {t}" if t else "⚠"

    style_el = pm.find("kml:Style", namespaces=KML_NS)
    if style_el is None:
        style_el = _sub(pm, "Style")

    # IconStyle
    icon_style = _ensure_path(style_el, ["IconStyle"])
    _ensure(icon_style, "scale").text = str(STALE_ICON_SCALE)
    _ensure(icon_style, "color").text = STALE_ICON_KML_COLOR
    icon = _ensure_path(icon_style, ["Icon"])
    _ensure(icon, "href").text = stale_href

    # LabelStyle
    label_style = _ensure_path(style_el, ["LabelStyle"])
    _ensure(label_style, "scale").text = str(STALE_LABEL_SCALE)

def flag_stale_placemarks_in_kml(kml_xml: str, base_url: str) -> str:
    root = ET.fromstring(kml_xml)
    doc = root.find("kml:Document", KML_NS)
    if doc is None:
        return kml_xml

    now = time.time()
    stale_href = f"{base_url}{STALE_ICON_PATH}"

    for pm in doc.findall(".//kml:Placemark", namespaces=KML_NS):
        gdh = extract_gdh_epoch_from_placemark(ET.tostring(pm, encoding="unicode"))
        if gdh and (now - gdh) > STALE_THRESHOLD_SECONDS:
            apply_stale_style(pm, stale_href)
            mark_pm_stale(pm)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")

# -------------------------
# KMZ icon embedding
# -------------------------
def extract_icon_urls(kml: str) -> list[str]:
    hrefs = re.findall(HREF_RE, kml, flags=re.IGNORECASE)
    out, seen = [], set()
    for h in map(str.strip, hrefs):
        lh = h.lower()
        if h.startswith(ALLOWED_ICON_PREFIX) and any(ext in lh for ext in (".png", ".jpg", ".jpeg")):
            if h not in seen:
                out.append(h); seen.add(h)
    return out
def move_stale_items_to_inativos_folder(kml_xml: str, folder_name: str = "inativos") -> str:
    """
    Moves:
      - stale Placemarks (ExtendedData/Data[@name='stale']/value='true') into Folder 'inativos'
      - Folders that contain at least one stale Placemark into Folder 'inativos'
    """
    root = ET.fromstring(kml_xml)
    doc = root.find("kml:Document", KML_NS)
    if doc is None:
        return kml_xml

    def is_stale_pm(pm: ET.Element) -> bool:
        v = pm.findtext(".//kml:ExtendedData/kml:Data[@name='stale']/kml:value", default="", namespaces=KML_NS)
        return (v or "").strip().lower() == "true"

    def folder_contains_stale(folder: ET.Element) -> bool:
        for pm in folder.findall(".//kml:Placemark", namespaces=KML_NS):
            if is_stale_pm(pm):
                return True
        return False

    # Find or create the inativos folder (top-level under Document)
    inativos = None
    for el in list(doc):
        if el.tag == f"{{{KML_NS_URI}}}Folder":
            nm = el.findtext("kml:name", default="", namespaces=KML_NS).strip()
            if nm.lower() == folder_name.lower():
                inativos = el
                break

    if inativos is None:
        inativos = ET.Element(f"{{{KML_NS_URI}}}Folder")
        ET.SubElement(inativos, f"{{{KML_NS_URI}}}name").text = folder_name

    # Rebuild document children with stale moved into inativos
    children = list(doc)
    new_children = []
    moved_any = False

    for el in children:
        if el is inativos:
            # we'll append it at the end (or later) once
            continue

        if el.tag == f"{{{KML_NS_URI}}}Placemark":
            if is_stale_pm(el):
                inativos.append(el)
                moved_any = True
            else:
                new_children.append(el)
            continue

        if el.tag == f"{{{KML_NS_URI}}}Folder":
            if folder_contains_stale(el):
                inativos.append(el)
                moved_any = True
            else:
                new_children.append(el)
            continue

        # Document metadata and other elements
        new_children.append(el)

    # Put inativos back only if we moved something (or if it already existed)
    if moved_any or inativos in children:
        new_children.append(inativos)

    doc.clear()
    for el in new_children:
        doc.append(el)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")

def safe_icon_filename(url: str) -> str:
    path = urlparse(url).path
    ext = ".jpg" if path.lower().endswith((".jpg", ".jpeg")) else ".png"
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return f"icons/{h}{ext}"

def fetch_icon_bytes(url: str) -> tuple[bytes, str]:
    now = time.time()
    cached = _icon_cache.get(url)
    if cached and (now - cached[0] < ICON_CACHE_SECONDS):
        return cached[1], cached[2]

    if not url.startswith(ALLOWED_ICON_PREFIX):
        raise ValueError("Icon URL not allowed")

    verify = False if ALLOW_INSECURE_SSL else certifi.where()
    r = requests.get(
        url, timeout=10,
        headers={
            "User-Agent": "KML-Proxy/ATAK-1.2",
            "Accept": "image/*,*/*;q=0.8",
            "Referer": "https://mapmil.igeoe.pt/"
        },
        verify=verify
    )
    r.raise_for_status()
    data = r.content
    ctype = r.headers.get("Content-Type", "image/png")
    _icon_cache[url] = (now, data, ctype)
    return data, ctype

def rewrite_kml_hrefs_to_embedded(kml: str, mapping: dict[str, str]) -> str:
    def repl(m):
        url = m.group(1).strip()
        return m.group(0).replace(url, mapping[url]) if url in mapping else m.group(0)
    return re.sub(HREF_RE, repl, kml, flags=re.IGNORECASE)

def build_kmz_with_embedded_icons(kml: str, base_url: str) -> bytes:
    icon_urls = extract_icon_urls(kml)
    url_to_path = {u: safe_icon_filename(u) for u in icon_urls}

    stale_href = f"{base_url}{STALE_ICON_PATH}"
    url_to_path[stale_href] = STALE_ICON_EMBED_PATH

    kml_rewritten = rewrite_kml_hrefs_to_embedded(kml, url_to_path)

    debug_lines = [
        f"Found icon URLs: {len(icon_urls)}",
        f"Stale href expected: {stale_href}",
        f"Stale icon file exists: {os.path.exists(STALE_ICON_FILE)}",
        "",
        "First 10 icon URLs:",
        *icon_urls[:10]
    ] if icon_urls else [
        "Found icon URLs: 0",
        "No icons matched. Check HREF_RE and ALLOWED_ICON_PREFIX.",
        f"HREF_RE: {HREF_RE}",
        f"ALLOWED_ICON_PREFIX: {ALLOWED_ICON_PREFIX}",
    ]

    icon_list_lines, error_lines = [], []
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml_rewritten)
        z.writestr("debug/_summary.txt", "\n".join(debug_lines))

        for url, embedded_path in url_to_path.items():
            icon_list_lines.append(f"{embedded_path} <= {url}")

            if url == stale_href:
                try:
                    if os.path.exists(STALE_ICON_FILE):
                        with open(STALE_ICON_FILE, "rb") as f:
                            z.writestr(embedded_path, f.read())
                    else:
                        error_lines.append(f"{embedded_path} <= {url}\n  ERROR: stale.png missing at {STALE_ICON_FILE}")
                except Exception as e:
                    error_lines.append(f"{embedded_path} <= {url}\n  ERROR: {type(e).__name__}: {e}")
                continue

            try:
                data, _ = fetch_icon_bytes(url)
                z.writestr(embedded_path, data)
            except Exception as e:
                error_lines.append(f"{embedded_path} <= {url}\n  ERROR: {type(e).__name__}: {e}")

        z.writestr("debug/_icon_list.txt", "\n".join(icon_list_lines) if icon_list_lines else "No icon URLs found.")
        if error_lines:
            z.writestr("debug/_download_errors.txt", "\n\n".join(error_lines))

    mem.seek(0)
    return mem.read()

def sort_kml_document_alphabetically(kml_xml: str) -> str:
    """
    Sorts:
      - Folders alphabetically by <name>
      - Standalone Placemarks alphabetically by <name>
      - Placemarks inside each Folder alphabetically
    """
    root = ET.fromstring(kml_xml)
    doc = root.find("kml:Document", KML_NS)
    if doc is None:
        return kml_xml

    def name_key(el: ET.Element) -> str:
        return (el.findtext("kml:name", default="", namespaces=KML_NS) or "").strip().lower()

    # Separate static (Document metadata) from sortable (Folders/Placemarks)
    children = list(doc)
    static_elements = []
    sortable_elements = []
    for el in children:
        if el.tag in (f"{{{KML_NS_URI}}}Folder", f"{{{KML_NS_URI}}}Placemark"):
            sortable_elements.append(el)
        else:
            static_elements.append(el)

    # Sort inside each Folder
    for folder in [e for e in sortable_elements if e.tag == f"{{{KML_NS_URI}}}Folder"]:
        folder_name = folder.findtext("kml:name", default="", namespaces=KML_NS)  # capture BEFORE clear

        folder_children = list(folder)
        placemarks = [c for c in folder_children if c.tag == f"{{{KML_NS_URI}}}Placemark"]
        others = [c for c in folder_children if c.tag != f"{{{KML_NS_URI}}}Placemark"]

        placemarks.sort(key=name_key)

        folder.clear()

        # Restore folder name exactly as it was (with original casing)
        name_el = ET.SubElement(folder, f"{{{KML_NS_URI}}}name")
        name_el.text = folder_name

        # Re-add any other folder-level elements (rare, but safe)
        for c in others:
            folder.append(c)

        # Re-add sorted placemarks
        for pm in placemarks:
            folder.append(pm)

    # Sort top-level
    sortable_elements.sort(key=name_key)

    # Rebuild doc
    doc.clear()
    for el in static_elements:
        doc.append(el)
    for el in sortable_elements:
        doc.append(el)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")

# -------------------------
# HTTP helpers + routes
# -------------------------
def make_error_kml(msg: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>KML Proxy Error</name>
    <Placemark>
      <name>Feed error</name>
      <description>{msg}</description>
      <Point><coordinates>0,0,0</coordinates></Point>
    </Placemark>
  </Document>
</kml>"""

def add_common_headers(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

def _base_url() -> str:
    return request.host_url.rstrip("/")

def kml_response(kml_body: str, content_type: str, disposition: str) -> Response:
    resp = Response(kml_body)
    resp.headers["Content-Type"] = content_type
    resp.headers["Content-Disposition"] = disposition
    return add_common_headers(resp)

def kmz_response(kmz_bytes: bytes, filename: str) -> Response:
    resp = Response(kmz_bytes)
    resp.headers["Content-Type"] = "application/vnd.google-earth.kmz"
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return add_common_headers(resp)

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)

@app.route("/debug/icons")
def debug_icons():
    try:
        kml_body = get_merged_kml_cached()
        icon_urls = extract_icon_urls(kml_body)
        lines = [
            f"Icons found: {len(icon_urls)}",
            f"ALLOWED_ICON_PREFIX: {ALLOWED_ICON_PREFIX}",
            f"HREF_RE: {HREF_RE}",
            "",
            "First 20 icon URLs:",
            *icon_urls[:20]
        ]
        return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")
    except Exception as e:
        return Response(str(e), mimetype="text/plain; charset=utf-8", status=500)

@app.route("/mapmil")
def mapmil_inline():
    try:
        body = flag_stale_placemarks_in_kml(get_merged_kml_cached(), _base_url())
        body = group_route_placemarks_into_folders(body)
        body = move_stale_items_to_inativos_folder(body, "0_Inativos")
        body = sort_kml_document_alphabetically(body)

        return kml_response(body, "application/xml; charset=utf-8", "inline; filename=mapmil.kml")
    except Exception as e:
        return kml_response(make_error_kml(str(e)), "application/xml; charset=utf-8", "inline; filename=mapmil.kml")

@app.route("/mapmil.kml")
def mapmil_download_kml():
    try:
        body = flag_stale_placemarks_in_kml(get_merged_kml_cached(), _base_url())
        body = group_route_placemarks_into_folders(body)
        body = move_stale_items_to_inativos_folder(body, "0_Inativos")
        body = sort_kml_document_alphabetically(body)
        return kml_response(body, "application/vnd.google-earth.kml+xml; charset=utf-8", "attachment; filename=mapmil.kml")
    except Exception as e:
        return kml_response(make_error_kml(str(e)), "application/vnd.google-earth.kml+xml; charset=utf-8", "attachment; filename=mapmil.kml")

@app.route("/mapmil.kmz")
def mapmil_download_kmz_simple():
    try:
        kml_body = flag_stale_placemarks_in_kml(get_merged_kml_cached(), _base_url())
        body = group_route_placemarks_into_folders(body)
        body = sort_kml_document_alphabetically(body)
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("doc.kml", kml_body)
        return kmz_response(mem.getvalue(), "mapmil.kmz")
    except Exception as e:
        error_kml = make_error_kml(str(e))
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("doc.kml", error_kml)
        return kmz_response(mem.getvalue(), "mapmil.kmz")

@app.route("/mapmil_atak.kmz")
def mapmil_download_kmz_atak():
    try:
        base_url = _base_url()
        kml_body = flag_stale_placemarks_in_kml(get_merged_kml_cached(), base_url)
        kml_body = group_route_placemarks_into_folders(kml_body)
        kml_body = move_stale_items_to_inativos_folder(kml_body, "0_Inativos")
        kml_body = sort_kml_document_alphabetically(kml_body)
        kmz_bytes = build_kmz_with_embedded_icons(kml_body, base_url)

        return kmz_response(kmz_bytes, "mapmil_atak.kmz")
    except Exception as e:
        base_url = _base_url()
        error_kml = flag_stale_placemarks_in_kml(make_error_kml(str(e)), base_url)
        kmz_bytes = build_kmz_with_embedded_icons(error_kml, base_url)
        return kmz_response(kmz_bytes, "mapmil_atak.kmz")

@app.route("/")
def index():
    return "OK | /debug/icons | /mapmil | /mapmil.kml | /mapmil.kmz | /mapmil_atak.kmz"

if __name__ == "__main__":
    os.makedirs(STATIC_DIR, exist_ok=True)
    app.run(host="0.0.0.0", port=8000)
