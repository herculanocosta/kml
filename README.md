# MapMil KML Proxy & ATAK KMZ Generator

A Flask-based KML proxy and transformer for MapMil feeds.

This service:

* Merges **recent** and **full** MapMil KML feeds
* Deduplicates placemarks based on ID and timestamp (GDH)
* Detects and flags **stale units**
* Groups routes with their base placemarks
* Sorts folders and placemarks alphabetically
* Generates:

  * Clean `.kml`
  * Simple `.kmz`
  * ATAK-ready `.kmz` with embedded icons

Designed for use with:

* Google Earth
* ATAK / WinTAK / iTAK
* Other KML/KMZ-compatible GIS tools

---

# ✨ Features

## 🔁 Dual-Source Merge

Merges two MapMil feeds:

* `SOURCE_KML_RECENT`
* `SOURCE_KML_FULL`

Deduplication logic:

* Prefer newer GDH timestamp
* Tie-break by source priority (recent > full)
* Fallback hash based on name + coordinates if no ID

---

## ⚠ Stale Unit Detection

If the GDH timestamp exceeds:

```
STALE_THRESHOLD_SECONDS = 3600
```

The placemark is:

* Prefixed with `⚠`
* Re-styled with faded icon
* Marked in ExtendedData:

  ```xml
  <Data name="stale">
      <value>true</value>
  </Data>
  ```
* Moved to folder: `0_Inativos`

---

## 📂 Route Grouping

Automatically groups:

```
<Placemark id="unit">
<Placemark id="unit_route">
```

Into:

```
<Folder>
   <name>Unit Name</name>
   unit
   unit_route
</Folder>
```

---

## 🔠 Alphabetical Sorting

Sorts:

* Top-level folders
* Standalone placemarks
* Placemarks inside folders

Preserves metadata and structure.

---

## 📦 KMZ with Embedded Icons (ATAK Mode)

`/mapmil_atak.kmz`:

* Downloads remote icon URLs
* Embeds icons into KMZ
* Rewrites `<href>` references
* Embeds stale icon locally
* Includes debug logs inside KMZ:

  * `debug/_summary.txt`
  * `debug/_icon_list.txt`
  * `debug/_download_errors.txt`

Optimized for ATAK environments that cannot fetch remote icons.

---

# 🚀 Installation

### 1. Clone repository

```bash
git clone https://github.com/yourusername/mapmil-proxy.git
cd mapmil-proxy
```

### 2. Install dependencies

```bash
pip install flask requests certifi
```

### 3. Run server

```bash
python app.py
```

Server runs at:

```
http://0.0.0.0:8000
```

---

# 🌐 Available Endpoints

| Endpoint           | Description                        |
| ------------------ | ---------------------------------- |
| `/`                | Health check                       |
| `/debug/icons`     | Shows detected icon URLs           |
| `/mapmil`          | Inline merged KML                  |
| `/mapmil.kml`      | Download KML                       |
| `/mapmil.kmz`      | Simple KMZ (no embedded icons)     |
| `/mapmil_atak.kmz` | ATAK-ready KMZ with embedded icons |

---

# ⚙ Configuration

Modify in script:

```python
SOURCE_KML_RECENT
SOURCE_KML_FULL
```

### Caching

```
CACHE_SECONDS = 10
ICON_CACHE_SECONDS = 3600
```

### Stale Detection

```
STALE_THRESHOLD_SECONDS = 3600
```

### Icon Security

Only allows icons from:

```
ALLOWED_ICON_PREFIX
```

---

# 🛡 Security Notes

* SSL verification enabled by default
* Optional insecure mode:

```python
ALLOW_INSECURE_SSL = True
```

* Icon fetching restricted by URL prefix
* No-store headers prevent client caching

---

# 🗂 Project Structure

```
.
├── app.py
├── static/
│   └── stale.png
```

`stale.png` is required for stale icon rendering and KMZ embedding.

---

# 🧠 How It Works (Pipeline)

1. Fetch both KML feeds
2. Normalize styles
3. Merge + dedupe
4. Flag stale units
5. Group routes
6. Move stale items to folder
7. Sort alphabetically
8. (Optional) Embed icons into KMZ

---

# 🧩 Designed For

* Military map overlays
* Operational tracking dashboards
* ATAK integration
* Tactical visualization feeds
* Situational awareness systems

---

# 📌 Requirements

* Python 3.10+
* Flask
* requests
* certifi

---

# 📄 License


