# splitwise_app.py
# pip install streamlit pdfplumber pandas openpyxl
# streamlit run splitwise_app.py

import re
import math
from io import BytesIO
from typing import Dict, List, Union, Optional

import pdfplumber
import pandas as pd
import streamlit as st

# ============================================================
#  SHARED SPLIT ENGINE
# ============================================================

SplitSpec = Union[List[str], Dict[str, float]]


def to_cents(x) -> int:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0
    return int(round(float(x) * 100))


def from_cents(c: int) -> float:
    return round(c / 100.0, 2)


def split_amount_cents(total_cents: int, weights: Dict[str, float]) -> Dict[str, int]:
    """Split total_cents by weights. Guarantees sum == total_cents."""
    if total_cents == 0:
        return {p: 0 for p in weights}
    weights = {p: w for p, w in weights.items() if w and w > 0}
    if not weights:
        return {}
    total_w = sum(weights.values())
    raw = {p: total_cents * (w / total_w) for p, w in weights.items()}
    base = {p: int(math.floor(v)) for p, v in raw.items()}
    remainder = total_cents - sum(base.values())
    frac_sorted = sorted(((p, raw[p] - base[p]) for p in weights), key=lambda t: t[1], reverse=True)
    for k in range(remainder):
        base[frac_sorted[k % len(frac_sorted)][0]] += 1
    return base


def normalize_split_spec(spec: Optional[SplitSpec], participants: List[str]) -> Dict[str, float]:
    if spec is None:
        return {p: 1.0 for p in participants}
    if isinstance(spec, list):
        return {p: 1.0 for p in spec}
    if isinstance(spec, dict):
        return {p: float(w) for p, w in spec.items()}
    raise ValueError("Invalid split spec")


def build_split(
    items_df: pd.DataFrame,
    participants: List[str],
    split_by_item: Dict[int, SplitSpec],
    default_split: SplitSpec,
    tax_cents: int,
    fee_cents: int,
    fee_split_spec: Optional[SplitSpec],
    paid_by: Optional[str] = None,
    amount_col: str = "amount",
    desc_col: str = "description",
):
    person_item_cents = {p: 0 for p in participants}
    detail_rows = []

    for ridx, row in items_df.iterrows():
        item_cents = to_cents(row[amount_col])
        spec = split_by_item.get(ridx, default_split)
        weights = normalize_split_spec(spec, participants)
        weights = {p: w for p, w in weights.items() if p in participants and w > 0}
        shares = split_amount_cents(item_cents, weights)
        for p, c in shares.items():
            person_item_cents[p] += c
            detail_rows.append({"row_type": "item", "description": row[desc_col], "person": p, "share": from_cents(c)})

    # Tax: proportional to item share
    person_tax_cents = {p: 0 for p in participants}
    if tax_cents > 0:
        prop_w = {p: float(person_item_cents[p]) for p in participants if person_item_cents[p] > 0}
        if not prop_w:
            prop_w = {p: 1.0 for p in participants}
        for p, c in split_amount_cents(tax_cents, prop_w).items():
            person_tax_cents[p] += c
            detail_rows.append({"row_type": "tax", "description": "Tax (proportional)", "person": p, "share": from_cents(c)})

    # Fee: split among selected people
    person_fee_cents = {p: 0 for p in participants}
    if fee_cents > 0 and fee_split_spec is not None:
        fee_w = normalize_split_spec(fee_split_spec, participants)
        fee_w = {p: w for p, w in fee_w.items() if p in participants and w > 0}
        if not fee_w:
            fee_w = {p: 1.0 for p in participants}
        for p, c in split_amount_cents(fee_cents, fee_w).items():
            person_fee_cents[p] += c
            detail_rows.append({"row_type": "fee", "description": "Fee", "person": p, "share": from_cents(c)})

    detail_df = pd.DataFrame(detail_rows)

    summary_rows = []
    for p in participants:
        ia = from_cents(person_item_cents[p])
        ta = from_cents(person_tax_cents[p])
        fa = from_cents(person_fee_cents[p])
        summary_rows.append({"person": p, "items": ia, "tax": ta, "fee": fa, "total_owes": round(ia + ta + fa, 2)})
    summary_df = pd.DataFrame(summary_rows)

    balances_df = None
    if paid_by and paid_by in participants:
        grand = sum(person_item_cents.values()) + tax_cents + fee_cents
        balances = []
        for p in participants:
            owes = person_item_cents[p] + person_tax_cents[p] + person_fee_cents[p]
            net = (grand - owes) if p == paid_by else -owes
            balances.append({"person": p, "net_balance": from_cents(net)})
        balances_df = pd.DataFrame(balances)

    return detail_df, summary_df, balances_df


def render_assignment_grid(items_df, participants, use_weights, fee_total, include_fee, tab_key,
                            amount_col="amount", desc_col="description",
                            qty_col="quantity", unit_col="unit_price"):
    """Renders the per-item checkbox/weight grid and returns (split_by_item, fee_split_spec)."""

    assign_df = items_df[[desc_col, qty_col, unit_col, amount_col]].copy()
    assign_df.columns = ["description", "quantity", "unit_price", "amount"]
    assign_df.insert(0, "row_id", assign_df.index.astype(str))
    assign_df.insert(1, "row_type", "item")

    if include_fee and fee_total > 0:
        assign_df = pd.concat([assign_df, pd.DataFrame([{
            "row_id": "FEE", "row_type": "fee",
            "description": "Fee – select who shares this",
            "quantity": "", "unit_price": "", "amount": fee_total,
        }])], ignore_index=True)

    for p in participants:
        assign_df[p] = True
    if use_weights:
        for p in participants:
            assign_df[f"w_{p}"] = 1.0

    col_cfg = {
        "row_id":      st.column_config.TextColumn("Row#", disabled=True, width="small"),
        "row_type":    st.column_config.TextColumn("Type", disabled=True, width="small"),
        "description": st.column_config.TextColumn("Item", disabled=True, width="large"),
        "quantity":    st.column_config.TextColumn("Qty",  disabled=True, width="small"),
        "unit_price":  st.column_config.TextColumn("Unit $", disabled=True, width="small"),
        "amount":      st.column_config.NumberColumn("Amount", disabled=True, format="$%.2f"),
    }
    for p in participants:
        col_cfg[p] = st.column_config.CheckboxColumn(p)
    if use_weights:
        for p in participants:
            col_cfg[f"w_{p}"] = st.column_config.NumberColumn(f"{p} wt", min_value=0.0, step=0.5, format="%.2f")

    edited = st.data_editor(assign_df, use_container_width=True, hide_index=True,
                            column_config=col_cfg, key=f"editor_{tab_key}")

    split_by_item: Dict[int, SplitSpec] = {}
    fee_split_spec: Optional[SplitSpec] = None

    for _, r in edited.iterrows():
        rtype = str(r["row_type"])
        selected = [p for p in participants if bool(r.get(p, False))] or participants[:]
        if rtype == "item":
            ridx = int(r["row_id"])
            if use_weights:
                wts = {p: float(r.get(f"w_{p}", 1.0)) for p in selected if float(r.get(f"w_{p}", 1.0)) > 0}
                split_by_item[ridx] = wts if wts else selected
            else:
                split_by_item[ridx] = selected
        elif rtype == "fee":
            if use_weights:
                wts = {p: float(r.get(f"w_{p}", 1.0)) for p in selected if float(r.get(f"w_{p}", 1.0)) > 0}
                fee_split_spec = wts if wts else selected
            else:
                fee_split_spec = selected

    if include_fee and fee_total > 0 and fee_split_spec is None:
        fee_split_spec = participants

    return split_by_item, fee_split_spec


def render_results(detail_df, summary_df, balances_df, prefix=""):
    r1, r2 = st.columns(2, gap="large")
    with r1:
        st.subheader("💰 Summary — what each person owes")
        st.dataframe(summary_df, use_container_width=True, hide_index=True)
        st.download_button("⬇️ Download summary CSV",
                           data=summary_df.to_csv(index=False).encode(),
                           file_name=f"{prefix}summary.csv", mime="text/csv")
    with r2:
        st.subheader("🧾 Detail — line-by-line shares")
        st.dataframe(detail_df, use_container_width=True, hide_index=True)
        st.download_button("⬇️ Download detail CSV",
                           data=detail_df.to_csv(index=False).encode(),
                           file_name=f"{prefix}detail.csv", mime="text/csv")
    if balances_df is not None:
        st.subheader("⚖️ Balances — one person paid")
        st.caption("Positive = gets money back · Negative = owes money")
        st.dataframe(balances_df, use_container_width=True, hide_index=True)
        st.download_button("⬇️ Download balances CSV",
                           data=balances_df.to_csv(index=False).encode(),
                           file_name=f"{prefix}balances.csv", mime="text/csv")


# ============================================================
#  SAM'S CLUB — PDF parser
# ============================================================

def extract_pdf_lines(uploaded_file) -> list[str]:
    data = uploaded_file.read()
    bio = BytesIO(data)
    lines = []
    with pdfplumber.open(bio) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.split("\n"):
                line = re.sub(r"\s+", " ", line).strip()
                if line:
                    lines.append(line)
    return lines


def parse_sams_receipt(lines: list[str]) -> pd.DataFrame:
    def clean_money(s):
        return float(s.replace("$", "").strip())

    def is_money_line(s):
        return re.fullmatch(r"\$?\d+\.\d{2}", s.strip()) is not None

    def money_from_line(s):
        m = re.findall(r"\$?\d+\.\d{2}", s)
        return clean_money(m[-1]) if m else None

    def is_item_line(s):
        return (" Qty " in s) and (money_from_line(s) is not None)

    item_pattern = re.compile(r"^(?P<item>.+?)\s+Qty\s+(?P<qty>\d+)\s+\$?(?P<total>\d+\.\d{2})$")
    rows = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        low = line.lower()
        if is_item_line(line):
            m = item_pattern.match(line)
            if m:
                qty = int(m.group("qty"))
                total = float(m.group("total"))
                rows.append({"type": "item", "description": m.group("item").strip(),
                             "quantity": qty, "unit_price": round(total / max(qty, 1), 2),
                             "amount": total, "raw_text": line})
        elif low == "subtotal":
            amt = money_from_line(line)
            if amt is None and i + 1 < len(lines) and is_money_line(lines[i + 1]):
                amt = clean_money(lines[i + 1]); i += 1
            if amt is not None:
                rows.append({"type": "subtotal", "description": "Subtotal", "quantity": None,
                             "unit_price": None, "amount": amt, "raw_text": line})
        elif low.startswith("tax"):
            amt = money_from_line(line)
            if amt is None and i + 1 < len(lines) and is_money_line(lines[i + 1]):
                amt = clean_money(lines[i + 1]); i += 1
            if amt is not None:
                rows.append({"type": "tax", "description": "Tax", "quantity": None,
                             "unit_price": None, "amount": amt, "raw_text": line})
        elif ("fee" in low or "deposit" in low) and "driver tip" not in low:
            amt = money_from_line(line)
            if amt is None and low == "beverage container deposit" and i + 3 < len(lines):
                mz, mu, mf = lines[i+1].strip(), lines[i+2].strip(), lines[i+3].strip()
                if is_money_line(mz) and re.match(r"^[a-f0-9\-]{30,}$", mu, re.I) and is_money_line(mf):
                    amt = clean_money(mf); i += 3
            if amt is None:
                for j in range(1, 4):
                    if i + j < len(lines) and is_money_line(lines[i + j]):
                        amt = clean_money(lines[i + j]); i += j; break
            if amt is not None:
                rows.append({"type": "fee", "description": "Additional Fee", "quantity": None,
                             "unit_price": None, "amount": amt, "raw_text": line})
        elif low.startswith("total"):
            amt = money_from_line(line)
            if amt is not None:
                rows.append({"type": "total", "description": "Total", "quantity": None,
                             "unit_price": None, "amount": amt, "raw_text": line})
            break
        i += 1
    return pd.DataFrame(rows)


# ============================================================
#  WALMART — XLSX parser
# ============================================================

def parse_walmart_xlsx(uploaded_file):
    data = uploaded_file.read()
    bio = BytesIO(data)
    raw = pd.read_excel(bio, sheet_name=0, header=0)

    items_mask = raw["Product Name"].notna() & raw["Delivery Status"].notna()
    items_df = raw[items_mask][["Product Name", "Quantity", "Price", "Delivery Status"]].copy().reset_index(drop=True)
    items_df.columns = ["description", "quantity", "amount", "delivery_status"]
    items_df["quantity"] = pd.to_numeric(items_df["quantity"], errors="coerce").fillna(0)
    items_df["amount"]   = pd.to_numeric(items_df["amount"],   errors="coerce").fillna(0)
    items_df["unit_price"] = items_df.apply(
        lambda r: round(r["amount"] / r["quantity"], 2) if r["quantity"] > 0 else r["amount"], axis=1)

    meta_mask = raw["Product Name"].notna() & raw["Delivery Status"].isna()
    meta_rows = raw[meta_mask][["Product Name", "Quantity"]].dropna(subset=["Product Name"])
    order_meta = dict(zip(meta_rows["Product Name"].astype(str), meta_rows["Quantity"].astype(str)))
    return items_df, order_meta


def get_meta_float(order_meta, key):
    try:
        return float(order_meta.get(key, 0))
    except Exception:
        return 0.0


# ============================================================
#  MAIN APP
# ============================================================

st.set_page_config(page_title="🛒 Grocery Splitwise", layout="wide")
st.title("🛒 Grocery Bill Splitter")
st.caption("Split Sam's Club (PDF) or Walmart (XLSX) orders among your group — Splitwise style.")

tab_sams, tab_walmart = st.tabs(["🏪 Sam's Club (PDF)", "🛍️ Walmart (XLSX)"])


# ============================================================
#  TAB 1 — SAM'S CLUB
# ============================================================
with tab_sams:
    st.header("Sam's Club PDF → Splitwise Splitter")

    with st.sidebar:
        st.markdown("---")
        st.subheader("🏪 Sam's Club Settings")
        sc_uploaded    = st.file_uploader("Upload Sam's Club PDF", type=["pdf"], key="sc_pdf")
        sc_names       = st.text_input("People (comma-separated)",
                                       value="Vinay Vaida, Vinay Aleti, Rohan Sattarapu, Enugu Dheekshith, Yeturi Bheem, Srinu bro",
                                       key="sc_names")
        sc_participants = [n.strip() for n in sc_names.split(",") if n.strip()]
        sc_tax         = st.checkbox("Include Tax",            value=True,  key="sc_tax")
        sc_fee         = st.checkbox("Include Additional Fee", value=True,  key="sc_fee")
        sc_weights     = st.checkbox("Enable weights",         value=False, key="sc_wt")
        sc_paid_by     = st.selectbox("Paid by", ["(none)"] + sc_participants, key="sc_paid")
        sc_paid_by     = None if sc_paid_by == "(none)" else sc_paid_by

    if not sc_uploaded:
        st.info("👈 Upload a Sam's Club PDF in the sidebar to begin.")
    else:
        lines = extract_pdf_lines(sc_uploaded)
        df    = parse_sams_receipt(lines)

        def get_amt(t):
            s = df.loc[df["type"] == t, "amount"]
            return float(s.iloc[0]) if len(s) else 0.0

        if sc_tax and (df["type"] == "tax").sum() == 0:
            st.warning("⚠️ Tax not detected.")
            with st.expander("Show extracted raw lines"):
                st.write(lines)

        colA, colB = st.columns(2, gap="large")
        with colA:
            st.subheader("📄 Parsed Receipt")
            st.dataframe(df, use_container_width=True, hide_index=True)
        with colB:
            st.subheader("Totals Check")
            st.write(f"Subtotal: **${get_amt('subtotal'):.2f}**")
            st.write(f"Fee:      **${get_amt('fee'):.2f}**")
            st.write(f"Tax:      **${get_amt('tax'):.2f}**")
            st.write(f"Total:    **${get_amt('total'):.2f}**")

        st.divider()
        sc_items_df = df[df["type"] == "item"].copy()
        fee_amt     = get_amt("fee")

        st.subheader("👥 Assign Items to People")
        st.caption("✅ Tax is split proportional to each person's item total. "
                   "✅ Fee row lets you pick who shares it.")

        # build assign grid manually (PDF items use different structure)
        assign_df = sc_items_df[["description", "quantity", "unit_price", "amount"]].copy()
        assign_df.insert(0, "row_id", assign_df.index.astype(str))
        assign_df.insert(1, "row_type", "item")

        if sc_fee and fee_amt > 0:
            assign_df = pd.concat([assign_df, pd.DataFrame([{
                "row_id": "FEE", "row_type": "fee",
                "description": "Fee – Additional Fee (select who shares this)",
                "quantity": "", "unit_price": "", "amount": fee_amt,
            }])], ignore_index=True)

        for p in sc_participants:
            assign_df[p] = True
        if sc_weights:
            for p in sc_participants:
                assign_df[f"w_{p}"] = 1.0

        col_cfg = {
            "row_id":      st.column_config.TextColumn("Row#", disabled=True, width="small"),
            "row_type":    st.column_config.TextColumn("Type", disabled=True, width="small"),
            "description": st.column_config.TextColumn("Item", disabled=True, width="large"),
            "quantity":    st.column_config.TextColumn("Qty",  disabled=True, width="small"),
            "unit_price":  st.column_config.TextColumn("Unit", disabled=True, width="small"),
            "amount":      st.column_config.NumberColumn("Amount", disabled=True, format="$%.2f"),
        }
        for p in sc_participants:
            col_cfg[p] = st.column_config.CheckboxColumn(p)
        if sc_weights:
            for p in sc_participants:
                col_cfg[f"w_{p}"] = st.column_config.NumberColumn(f"{p} wt", min_value=0.0, step=0.5, format="%.2f")

        sc_edited = st.data_editor(assign_df, use_container_width=True, hide_index=True,
                                   column_config=col_cfg, key="sc_editor")

        sc_split_by_item: Dict[int, SplitSpec] = {}
        sc_fee_split_spec: Optional[SplitSpec] = None

        for _, r in sc_edited.iterrows():
            rtype    = str(r["row_type"])
            selected = [p for p in sc_participants if bool(r.get(p, False))] or sc_participants[:]
            if rtype == "item":
                ridx = int(r["row_id"])
                if sc_weights:
                    wts = {p: float(r.get(f"w_{p}", 1.0)) for p in selected if float(r.get(f"w_{p}", 1.0)) > 0}
                    sc_split_by_item[ridx] = wts if wts else selected
                else:
                    sc_split_by_item[ridx] = selected
            elif rtype == "fee":
                if sc_weights:
                    wts = {p: float(r.get(f"w_{p}", 1.0)) for p in selected if float(r.get(f"w_{p}", 1.0)) > 0}
                    sc_fee_split_spec = wts if wts else selected
                else:
                    sc_fee_split_spec = selected

        if sc_fee and fee_amt > 0 and sc_fee_split_spec is None:
            sc_fee_split_spec = sc_participants

        sc_tax_cents = to_cents(get_amt("tax")) if sc_tax else 0
        sc_fee_cents = to_cents(fee_amt)        if sc_fee else 0

        sc_detail, sc_summary, sc_balances = build_split(
            items_df      = sc_items_df,
            participants  = sc_participants,
            split_by_item = sc_split_by_item,
            default_split = sc_participants,
            tax_cents     = sc_tax_cents,
            fee_cents     = sc_fee_cents,
            fee_split_spec= sc_fee_split_spec,
            paid_by       = sc_paid_by,
        )

        st.divider()
        render_results(sc_detail, sc_summary, sc_balances, prefix="sams_")


# ============================================================
#  TAB 2 — WALMART
# ============================================================
with tab_walmart:
    st.header("Walmart XLSX → Splitwise Splitter")

    with st.sidebar:
        st.markdown("---")
        st.subheader("🛍️ Walmart Settings")
        wm_uploaded   = st.file_uploader("Upload Walmart order .xlsx", type=["xlsx"], key="wm_xlsx")
        wm_filter     = st.radio("Show items:", ["Delivered only", "All items"], key="wm_filter")
        wm_names      = st.text_input("People (comma-separated)",
                                      value="Vinay Vaida, Vinay Aleti, Rohan Sattarapu, Enugu Dheekshith, Yeturi Bheem, Srinu bro",
                                      key="wm_names")
        wm_participants = [n.strip() for n in wm_names.split(",") if n.strip()]
        wm_tax        = st.checkbox("Include Tax",             value=True,  key="wm_tax")
        wm_fee        = st.checkbox("Include Bag/Delivery Fee",value=True,  key="wm_fee")
        wm_weights    = st.checkbox("Enable weights",          value=False, key="wm_wt")
        wm_paid_by    = st.selectbox("Paid by", ["(none)"] + wm_participants, key="wm_paid")
        wm_paid_by    = None if wm_paid_by == "(none)" else wm_paid_by

    if not wm_uploaded:
        st.info("👈 Upload a Walmart order .xlsx in the sidebar to begin.")
    else:
        all_items_df, order_meta = parse_walmart_xlsx(wm_uploaded)

        if wm_filter == "Delivered only":
            items_df = all_items_df[
                all_items_df["delivery_status"].str.contains("delivered", case=False, na=False)
            ].reset_index(drop=True)
        else:
            items_df = all_items_df.reset_index(drop=True)

        tax_val   = get_meta_float(order_meta, "Tax")
        bag_fee   = get_meta_float(order_meta, "Bag Fee")
        deliv_fee = get_meta_float(order_meta, "Delivery Charges")
        total_fee = round(bag_fee + deliv_fee, 2)
        order_total = get_meta_float(order_meta, "Order Total")
        subtotal    = get_meta_float(order_meta, "Subtotal")

        # Order Summary metrics
        st.subheader("📋 Order Summary")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Order #",    order_meta.get("Order Number", "—"))
        m2.metric("Order Date", order_meta.get("Order Date",   "—"))
        m3.metric("Subtotal",   f"${subtotal:.2f}")
        m4.metric("Tax",        f"${tax_val:.2f}")
        m5.metric("Order Total",f"${order_total:.2f}")

        st.divider()

        colA, colB = st.columns(2, gap="large")
        with colA:
            st.subheader(f"📦 Items ({len(items_df)} rows)")
            st.dataframe(
                items_df[["description","quantity","unit_price","amount","delivery_status"]],
                use_container_width=True, hide_index=True)
        with colB:
            st.subheader("Totals Check")
            st.write(f"Items subtotal:    **${items_df['amount'].sum():.2f}**")
            st.write(f"Tax:               **${tax_val:.2f}**")
            st.write(f"Bag Fee:           **${bag_fee:.2f}**")
            st.write(f"Delivery Charges:  **${deliv_fee:.2f}**")
            st.write(f"Order Total:       **${order_total:.2f}**")
            st.write("Delivery Status breakdown:")
            sc = all_items_df["delivery_status"].value_counts().reset_index()
            sc.columns = ["Status", "Count"]
            st.dataframe(sc, hide_index=True, use_container_width=True)

        st.divider()
        st.subheader("👥 Assign Items to People")
        st.caption("✅ Tax is split proportional to each person's item total. "
                   "✅ Fee row lets you pick who shares it.")

        wm_split_by_item, wm_fee_split_spec = render_assignment_grid(
            items_df      = items_df,
            participants  = wm_participants,
            use_weights   = wm_weights,
            fee_total     = total_fee,
            include_fee   = wm_fee,
            tab_key       = "walmart",
        )

        wm_tax_cents = to_cents(tax_val)   if wm_tax else 0
        wm_fee_cents = to_cents(total_fee) if wm_fee else 0

        wm_detail, wm_summary, wm_balances = build_split(
            items_df      = items_df,
            participants  = wm_participants,
            split_by_item = wm_split_by_item,
            default_split = wm_participants,
            tax_cents     = wm_tax_cents,
            fee_cents     = wm_fee_cents,
            fee_split_spec= wm_fee_split_spec,
            paid_by       = wm_paid_by,
        )

        st.divider()
        render_results(wm_detail, wm_summary, wm_balances, prefix="walmart_")
