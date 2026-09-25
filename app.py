"""
Streamlit dashboard for the unified eyewear dataset.
Run with:  streamlit run app.py
"""

from pathlib import Path

import pandas as pd
import streamlit as st

DATA_PATH = Path(__file__).resolve().parent / "extracted_data" / "unified_eyewear.csv"

SOURCE_LABELS = {
    "framesdirect": "FramesDirect",
    "eyebuydirect": "EyeBuyDirect",
}
SOURCE_URLS = {
    "framesdirect": "https://www.framesdirect.com/eyeglasses/",
    "eyebuydirect": "https://www.eyebuydirect.com/eyeglasses",
}
EXPECTED_SOURCES = set(SOURCE_LABELS)

# Columns unify.py is expected to produce
REQUIRED = ["source", "brand", "name", "former_price", "current_price",
            "discount", "product_link", "page"]
OPTIONAL = [
    "discount_pct",
    "savings",
    "on_sale",
    "sold_by_n_sites",
    "scraped_at",
    "collection_method",
]

st.set_page_config(page_title="Eyewear Price Explorer", layout="wide")


def options(series):
    """Sorted unique values, safe against nulls and mixed types."""
    return sorted(series.dropna().astype(str).unique())


@st.cache_data
def load_data(path):
    df = pd.read_csv(path)

    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        return None, f"Missing required columns: {', '.join(missing)}"

    # A row with no provenance can't support retailer comparison
    dropped_source = int(df["source"].isna().sum())
    df = df[df["source"].notna()].copy()

    df["retailer"] = (df["source"].astype(str)
                                  .map(SOURCE_LABELS)
                                  .fillna(df["source"].astype(str)))

    # Prices must be numeric for the slider and every aggregate
    for col in ("former_price", "current_price", "savings", "discount_pct"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    dropped_price = int(df["current_price"].isna().sum())
    df = df[df["current_price"].notna()].copy()

    # Fill in anything the unifier didn't derive, so the app never crashes
    if "on_sale" not in df.columns:
        df["on_sale"] = (df["former_price"].notna()
                         & (df["former_price"] > df["current_price"]))
    df["on_sale"] = df["on_sale"].fillna(False).astype(bool)

    if "sold_by_n_sites" not in df.columns:
        key = (df["brand"].fillna("").astype(str).str.lower().str.strip() + "|"
               + df["name"].fillna("").astype(str).str.lower().str.strip())
        df["sold_by_n_sites"] = key.map(df.groupby(key)["source"].nunique())
    df["sold_by_n_sites"] = pd.to_numeric(
        df["sold_by_n_sites"], errors="coerce").fillna(1).astype(int)

    if "scraped_at" not in df.columns:
        df["scraped_at"] = "unknown"
    if "collection_method" not in df.columns:
        df["collection_method"] = "unknown"

    df["brand"] = df["brand"].fillna("Unbranded").astype(str)
    df["name"] = df["name"].fillna("").astype(str)

    notes = []
    if dropped_source:
        notes.append(f"{dropped_source} rows had no source and were dropped")
    if dropped_price:
        notes.append(f"{dropped_price} rows had no price and were dropped")

    return df, notes


if not DATA_PATH.exists():
    st.error(f"No data at {DATA_PATH}. Run the scrapers, then unify.py.")
    st.stop()

data, notes = load_data(DATA_PATH)

if data is None:
    st.error(notes)
    st.caption("Check that unify.py wrote the full output contract.")
    st.stop()

if data.empty:
    st.error("The unified file loaded but contains no usable rows.")
    st.stop()

st.title("Eyewear Price Explorer")
st.caption("Prices collected from public catalogue pages. See Sources below.")

if notes:
    st.warning("Data quality: " + "; ".join(notes))

missing_sources = sorted(EXPECTED_SOURCES - set(data["source"].astype(str)))
if missing_sources:
    missing_labels = [SOURCE_LABELS.get(source, source) for source in missing_sources]
    st.warning(
        "Partial dataset: no usable rows were collected from "
        + ", ".join(missing_labels)
        + ". Cross-retailer comparisons are incomplete."
    )

# ------------------------------- Filters -------------------------------------
with st.sidebar:
    st.header("Filters")

    retailer_options = options(data["retailer"])
    retailers = st.multiselect("Retailer", retailer_options,
                               default=retailer_options)

    brands = st.multiselect("Brand", options(data["brand"]))

    lo = float(data["current_price"].min())
    hi = float(data["current_price"].max())
    if lo == hi:
        hi = lo + 1.0  # slider needs a non-zero range
    price_range = st.slider("Price range ($)", lo, hi, (lo, hi))

    sale_only = st.checkbox("On sale only")
    search = st.text_input("Search name")

view = data[data["retailer"].isin(retailers)]
if brands:
    view = view[view["brand"].isin(brands)]
view = view[view["current_price"].between(*price_range)]
if sale_only:
    view = view[view["on_sale"]]
if search:
    view = view[view["name"].str.contains(search, case=False, na=False)]

# ------------------------------- Summary -------------------------------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Frames", f"{len(view):,}")
c2.metric("Brands", view["brand"].nunique())
c3.metric("Median price", f"${view['current_price'].median():,.0f}" if len(view) else "-")
c4.metric("On sale", f"{view['on_sale'].mean() * 100:.0f}%" if len(view) else "-")

st.divider()

tab_browse, tab_compare, tab_charts, tab_sources = st.tabs(
    ["Browse", "Cross-retailer", "Charts", "Sources"]
)

# ------------------------------- Browse --------------------------------------
with tab_browse:
    if view.empty:
        st.info("No frames match these filters.")
    else:
        st.dataframe(
            view[["retailer", "brand", "name", "former_price",
                  "current_price", "discount", "product_link"]],
            column_config={
                "retailer": "Retailer",
                "brand": "Brand",
                "name": "Model",
                "former_price": st.column_config.NumberColumn("Was", format="$%.2f"),
                "current_price": st.column_config.NumberColumn("Now", format="$%.2f"),
                "discount": "Discount",
                "product_link": st.column_config.LinkColumn("Link", display_text="View"),
            },
            hide_index=True,
            width="stretch",
        )
        st.download_button(
            "Download this view as CSV",
            view.to_csv(index=False).encode("utf-8"),
            "eyewear_filtered.csv",
            "text/csv",
        )

# --------------------------- Cross-retailer ----------------------------------
with tab_compare:
    st.subheader("Frames listed by more than one retailer")
    multi = view[view["sold_by_n_sites"] > 1]

    if multi.empty:
        st.info(
            "No overlapping products in the current selection. If you expected "
            "some, the two retailers likely name the same model differently."
        )
    else:
        pivot = multi.pivot_table(
            index=["brand", "name"],
            columns="retailer",
            values="current_price",
            aggfunc="min",
        )
        pivot["Spread"] = (pivot.max(axis=1) - pivot.min(axis=1)).round(2)
        pivot = pivot.sort_values("Spread", ascending=False)

        st.dataframe(pivot, width="stretch")
        st.caption(
            "Matched on brand and model name. Retailers name models "
            "inconsistently, so this is indicative rather than exhaustive."
        )

# ------------------------------- Charts --------------------------------------
with tab_charts:
    if view.empty:
        st.info("No data to chart.")
    else:
        left, right = st.columns(2)

        with left:
            st.subheader("Median price by retailer")
            st.bar_chart(view.groupby("retailer")["current_price"].median())

        with right:
            st.subheader("Listings by retailer")
            st.bar_chart(view["retailer"].value_counts())

        st.subheader("Price distribution")
        if view["current_price"].nunique() > 1:
            bins = pd.cut(view["current_price"], bins=20)
            dist = view.groupby(bins, observed=True).size()
            dist.index = dist.index.astype(str)
            st.bar_chart(dist)
        else:
            st.info("Not enough price variation to plot a distribution.")

        st.subheader("Top 15 brands")
        st.bar_chart(view["brand"].value_counts().head(15))

# ------------------------------- Sources -------------------------------------
with tab_sources:
    st.subheader("Where this data comes from")

    for src, group in data.groupby("source"):
        label = SOURCE_LABELS.get(src, src)
        url = SOURCE_URLS.get(src)
        collected = group["scraped_at"].dropna().max()
        methods = ", ".join(sorted(group["collection_method"].dropna().unique()))

        st.markdown(
            f"**{label}** — {len(group):,} frames, collected "
            f"{collected or 'unknown'} via {methods or 'unknown'}"
        )
        if url:
            st.markdown(f"Catalogue: {url}")
        st.markdown("")

    st.divider()
    st.markdown(
        "Every row links back to its own product page, and the `source` column "
        "records which retailer it came from. Prices were correct at collection "
        "time and change frequently; check the retailer before relying on any "
        "figure here."
    )
