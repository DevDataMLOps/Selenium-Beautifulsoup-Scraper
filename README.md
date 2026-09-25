# Frames & Lens Price Explorer

A Python capstone that collects eyeglass listings from **EyeBuyDirect** and
**FramesDirect**, parses the rendered catalogues with BeautifulSoup, combines
the results, and presents them in a Streamlit dashboard.

## What the project includes

- Selenium Grid browsers managed with Docker Compose
- Separate retailer scrapers with pagination and CSV/JSON output
- BeautifulSoup parsing of the browser-rendered HTML
- A unifier that cleans prices, removes duplicates, and derives sale metrics
- A Streamlit dashboard with retailer, brand, price, sale, and text filters
- Diagnostic HTML and screenshots when a catalogue cannot be read

## Requirements

- Python 3.11 or newer
- Docker Desktop with Docker Compose

Create and activate a virtual environment, then install the dependencies:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 1. Start Selenium Grid

```powershell
docker compose up -d
```

Confirm both browser nodes are ready:

```powershell
Invoke-RestMethod http://127.0.0.1:4444/status
Invoke-RestMethod http://127.0.0.1:4445/status
```

Chrome is available at port `4444`; Firefox is available at port `4445`.
Optional live browser views are available at `http://127.0.0.1:7900` and
`http://127.0.0.1:7901`.

## 2. Run the scrapers

For a quick one-page validation:

```powershell
python frames.py --max-pages 1
python eyebuy.py --max-pages 1
```

For the default multi-page collection:

```powershell
python frames.py
python eyebuy.py
```

Use `--headed` with either scraper to render through the Grid's virtual
display. The retailer exports are written to `extracted_data/` after every
completed page so an interrupted run preserves its progress.

### EyeBuyDirect access note

EyeBuyDirect currently returns a server-side **Access Denied** response to the
standard Chrome and Firefox Grid sessions tested for this project. The scraper
detects that response, saves diagnostics, exits non-zero, and does not attempt
to evade the retailer's access controls. A dated fallback export of 28 listings
verified against EyeBuyDirect's public catalogue on September 24, 2026 is
included so the analysis and dashboard remain reproducible. Its provenance is
recorded in `extracted_data/eyebuydirect_snapshot_metadata.json`; it must not be
described as a successful Grid scrape. A failed future scrape preserves the
last valid export instead of erasing it.

## 3. Build the unified dataset

```powershell
python unify.py
```

By default this command returns a non-zero status if either mandatory retailer
has no usable rows. For local dashboard development only, a partial file can be
accepted explicitly with `python unify.py --allow-partial`.

This creates:

- `extracted_data/unified_eyewear.csv`
- `extracted_data/unified_eyewear.json`

The unified data includes source provenance, brand, model, current/former
price, discount, product URL, catalogue page, savings, discount percentage,
sale status, cross-retailer match count, and collection timestamp.

## 4. Launch the dashboard

```powershell
python -m streamlit run app.py
```

The dashboard reads `extracted_data/unified_eyewear.csv` and provides browsing,
download, comparison, chart, and source-provenance views.

## Output contract

Both scrapers write the same base columns:

| Column | Meaning |
| --- | --- |
| `source` | Retailer identifier |
| `brand` | Displayed brand |
| `name` | Product/model name |
| `former_price` | Previous price when a real markdown exists |
| `current_price` | Current displayed frame price |
| `discount` | Calculated percentage label for sale items |
| `product_link` | Absolute product URL |
| `page` | Catalogue page number |

## Responsible use

Catalogue layouts, prices, and access rules change. Use conservative page
limits, review each retailer's current terms and robots policy, and do not use
this project to bypass access controls. Prices are snapshots and should be
verified on the retailer page before making decisions.

Stop and remove the Grid containers when finished:

```powershell
docker compose down
```
