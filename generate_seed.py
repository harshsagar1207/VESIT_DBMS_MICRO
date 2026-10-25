#!/usr/bin/env python3
"""Generates 03b_market_extension.sql and 04_seed.sql (Indian market universe).

Deterministic: same output on every run. Index membership comes from public sources checked
24 Oct 2026; everything numeric below (prices, coupons, NAVs, IPO terms) is synthetic demo data
and is tagged data_source = 'seed-synthetic' in the database.
"""
import hashlib
import sys
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")


def h(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16)


def q(s):
    return "NULL" if s is None else "'" + str(s).replace("'", "''") + "'"


# --------------------------------------------------------------------------- equities
# (symbol, name, sector, nifty50_weight or None, sensex_weight or None, banknifty_weight or None)
NIFTY = [
    ("ADANIENT", "Adani Enterprises Ltd.", "Metals & Mining", 0.78),
    ("ADANIPORTS", "Adani Ports and Special Economic Zone Ltd.", "Services", 1.13),
    ("APOLLOHOSP", "Apollo Hospitals Enterprise Ltd.", "Healthcare", 0.82),
    ("ASIANPAINT", "Asian Paints Ltd.", "Consumer Durables", 1.08),
    ("AXISBANK", "Axis Bank Ltd.", "Financial Services", 3.22),
    ("BAJAJ-AUTO", "Bajaj Auto Ltd.", "Automobile and Auto Components", 1.17),
    ("BAJAJFINSV", "Bajaj Finserv Ltd.", "Financial Services", 1.06),
    ("BAJFINANCE", "Bajaj Finance Ltd.", "Financial Services", 2.63),
    ("BEL", "Bharat Electronics Ltd.", "Capital Goods", 1.33),
    ("BHARTIARTL", "Bharti Airtel Ltd.", "Telecommunication", 5.30),
    ("CIPLA", "Cipla Ltd.", "Healthcare", 0.73),
    ("COALINDIA", "Coal India Ltd.", "Oil, Gas & Consumable Fuels", 0.87),
    ("DRREDDY", "Dr. Reddy's Laboratories Ltd.", "Healthcare", 0.65),
    ("EICHERMOT", "Eicher Motors Ltd.", "Automobile and Auto Components", 1.00),
    ("ETERNAL", "Eternal Ltd.", "Consumer Services", 2.10),
    ("GRASIM", "Grasim Industries Ltd.", "Construction Materials", 1.12),
    ("HCLTECH", "HCL Technologies Ltd.", "Information Technology", 1.27),
    ("HDFCBANK", "HDFC Bank Ltd.", "Financial Services", 10.01),
    ("HDFCLIFE", "HDFC Life Insurance Company Ltd.", "Financial Services", 0.52),
    ("HINDALCO", "Hindalco Industries Ltd.", "Metals & Mining", 1.37),
    ("HINDUNILVR", "Hindustan Unilever Ltd.", "Fast Moving Consumer Goods", 1.63),
    ("ICICIBANK", "ICICI Bank Ltd.", "Financial Services", 9.11),
    ("INDIGO", "InterGlobe Aviation Ltd.", "Services", 1.07),
    ("INFY", "Infosys Ltd.", "Information Technology", 3.56),
    ("ITC", "ITC Ltd.", "Fast Moving Consumer Goods", 2.34),
    ("JIOFIN", "Jio Financial Services Ltd.", "Financial Services", 0.72),
    ("JSWSTEEL", "JSW Steel Ltd.", "Metals & Mining", 1.09),
    ("KOTAKBANK", "Kotak Mahindra Bank Ltd.", "Financial Services", 2.61),
    ("LT", "Larsen & Toubro Ltd.", "Construction", 4.29),
    ("M&M", "Mahindra & Mahindra Ltd.", "Automobile and Auto Components", 2.77),
    ("MARUTI", "Maruti Suzuki India Ltd.", "Automobile and Auto Components", 1.62),
    ("MAXHEALTH", "Max Healthcare Institute Ltd.", "Healthcare", 0.67),
    ("NESTLEIND", "Nestle India Ltd.", "Fast Moving Consumer Goods", 0.95),
    ("NTPC", "NTPC Ltd.", "Power", 1.45),
    ("ONGC", "Oil & Natural Gas Corporation Ltd.", "Oil, Gas & Consumable Fuels", 0.84),
    ("POWERGRID", "Power Grid Corporation of India Ltd.", "Power", 1.08),
    ("RELIANCE", "Reliance Industries Ltd.", "Oil, Gas & Consumable Fuels", 8.04),
    ("SBILIFE", "SBI Life Insurance Company Ltd.", "Financial Services", 0.73),
    ("SBIN", "State Bank of India", "Financial Services", 3.93),
    ("SHRIRAMFIN", "Shriram Finance Ltd.", "Financial Services", 1.41),
    ("SUNPHARMA", "Sun Pharmaceutical Industries Ltd.", "Healthcare", 1.83),
    ("TATACONSUM", "Tata Consumer Products Ltd.", "Fast Moving Consumer Goods", 0.63),
    ("TATASTEEL", "Tata Steel Ltd.", "Metals & Mining", 1.38),
    ("TCS", "Tata Consultancy Services Ltd.", "Information Technology", 2.12),
    ("TECHM", "Tech Mahindra Ltd.", "Information Technology", 0.92),
    ("TITAN", "Titan Company Ltd.", "Consumer Durables", 1.90),
    ("TMPV", "Tata Motors Passenger Vehicles Ltd.", "Automobile and Auto Components", 0.61),
    ("TRENT", "Trent Ltd.", "Consumer Services", 0.89),
    ("ULTRACEMCO", "UltraTech Cement Ltd.", "Construction Materials", 1.23),
    # Wipro was replaced by BSE Ltd. on 30 Sep 2026; no published weight yet in the sources used.
    ("BSE", "BSE Ltd.", "Financial Services", None),
]
BANK_ONLY = [  # Nifty Bank members that are not in Nifty 50
    ("PNB", "Punjab National Bank", "Financial Services"),
    ("UNIONBANK", "Union Bank of India", "Financial Services"),
    ("BANKBARODA", "Bank of Baroda", "Financial Services"),
    ("CANBK", "Canara Bank", "Financial Services"),
    ("FEDERALBNK", "The Federal Bank Ltd.", "Financial Services"),
    ("AUBANK", "AU Small Finance Bank Ltd.", "Financial Services"),
    ("INDUSINDBK", "IndusInd Bank Ltd.", "Financial Services"),
    ("IDFCFIRSTB", "IDFC First Bank Ltd.", "Financial Services"),
    ("YESBANK", "Yes Bank Ltd.", "Financial Services"),
]
BANKNIFTY_W = {"HDFCBANK": 23.11, "ICICIBANK": 21.65, "SBIN": 19.33, "KOTAKBANK": 9.41, "AXISBANK": 8.13,
               "PNB": 3.04, "UNIONBANK": 3.00, "BANKBARODA": 2.63, "CANBK": 2.36, "FEDERALBNK": 1.86,
               "AUBANK": 1.54, "INDUSINDBK": 1.43, "IDFCFIRSTB": 1.40, "YESBANK": 1.09}
SENSEX_W = {"RELIANCE": 10.84, "BHARTIARTL": 8.07, "HDFCBANK": 7.28, "ICICIBANK": 6.82, "TCS": 6.17,
            "SBIN": 6.09, "BAJFINANCE": 3.98, "LT": 3.37, "INFY": 3.18, "KOTAKBANK": 2.96,
            "HINDUNILVR": 2.93, "SUNPHARMA": 2.87, "ADANIPORTS": 2.75, "TITAN": 2.64, "HCLTECH": 2.63,
            "AXISBANK": 2.56, "M&M": 2.42, "MARUTI": 2.40, "ITC": 2.20, "ULTRACEMCO": 2.20,
            "ETERNAL": 2.00, "NTPC": 1.99, "BAJAJFINSV": 1.84, "BEL": 1.83, "POWERGRID": 1.57,
            "ASIANPAINT": 1.50, "TATASTEEL": 1.47, "TECHM": 1.22, "INDIGO": 1.22, "TRENT": 1.01}

# (symbol, name, exchange, currency, tick, base level, amplitude)
INDICES = [
    ("NIFTY", "Nifty 50 Index", "NSE", "INR", 0.05, 24600, 0.030),
    ("BANKNIFTY", "Nifty Bank Index", "NSE", "INR", 0.05, 55000, 0.035),
    ("SENSEX", "BSE Sensex", "BSE", "INR", 0.01, 78000, 0.030),
]
GIFT = ("GIFTNIFTY", "GIFT Nifty (NSE IX Nifty 50 futures)", "NSE_IX", "USD", 0.05, 24600, 0.030)

# --------------------------------------------------------------------------- mutual funds
AMCS = ["SBI", "HDFC", "ICICI Prudential", "Nippon India", "Kotak", "Axis", "Aditya Birla Sun Life",
        "UTI", "Mirae Asset", "DSP", "Franklin India", "Tata", "Invesco India", "Motilal Oswal",
        "Canara Robeco"]
MF_CATS = [  # (category, SEBI group, expense ratio %, NAV lo, NAV span, amp)
    ("Large Cap", "Equity", 0.85, 40, 400, 0.020),
    ("Flexi Cap", "Equity", 0.80, 30, 500, 0.022),
    ("Mid Cap", "Equity", 0.75, 60, 600, 0.028),
    ("Small Cap", "Equity", 0.70, 40, 300, 0.034),
    ("ELSS Tax Saver", "Equity", 0.90, 30, 400, 0.022),
    ("Liquid", "Debt", 0.20, 1000, 3000, 0.0008),
    ("Corporate Bond", "Debt", 0.35, 15, 80, 0.002),
    ("Nifty 50 Index", "Index", 0.15, 100, 300, 0.030),
]

# --------------------------------------------------------------------------- bonds
STATES = [("MH", "Maharashtra"), ("TN", "Tamil Nadu"), ("GJ", "Gujarat"), ("KA", "Karnataka"),
          ("UP", "Uttar Pradesh"), ("RJ", "Rajasthan"), ("WB", "West Bengal"), ("TS", "Telangana"),
          ("AP", "Andhra Pradesh"), ("MP", "Madhya Pradesh"), ("KL", "Kerala"), ("PB", "Punjab"),
          ("HR", "Haryana"), ("BR", "Bihar"), ("OD", "Odisha"), ("AS", "Assam"),
          ("CG", "Chhattisgarh"), ("JH", "Jharkhand"), ("UK", "Uttarakhand"), ("GA", "Goa")]
CORP = [("REC", "REC Ltd.", "AAA"), ("PFC", "Power Finance Corporation Ltd.", "AAA"),
        ("NHAI", "National Highways Authority of India", "AAA"), ("NABARD", "NABARD", "AAA"),
        ("SIDBI", "SIDBI", "AAA"), ("EXIM", "Export-Import Bank of India", "AAA"),
        ("IRFC", "Indian Railway Finance Corporation Ltd.", "AAA"), ("HUDCO", "HUDCO", "AAA"),
        ("PGCIL", "Power Grid Corporation of India Ltd.", "AAA"), ("NTPC", "NTPC Ltd.", "AAA"),
        ("LICHF", "LIC Housing Finance Ltd.", "AAA"), ("HDFCB", "HDFC Bank Ltd.", "AAA"),
        ("BAJFIN", "Bajaj Finance Ltd.", "AAA"), ("BAJHFL", "Bajaj Housing Finance Ltd.", "AAA"),
        ("TATACAP", "Tata Capital Ltd.", "AAA"), ("LTFIN", "L&T Finance Ltd.", "AAA"),
        ("SHRIRAM", "Shriram Finance Ltd.", "AA+"), ("MUTHOOT", "Muthoot Finance Ltd.", "AA+"),
        ("MMFSL", "Mahindra & Mahindra Financial Services Ltd.", "AAA"),
        ("ABCAP", "Aditya Birla Capital Ltd.", "AAA"), ("SUNDARAM", "Sundaram Finance Ltd.", "AAA"),
        ("CHOLA", "Cholamandalam Investment and Finance Co. Ltd.", "AA+"),
        ("POONAWALLA", "Poonawalla Fincorp Ltd.", "AAA"), ("PIRAMAL", "Piramal Capital & Housing Finance Ltd.", "AA"),
        ("IOC", "Indian Oil Corporation Ltd.", "AAA"), ("ONGCB", "Oil & Natural Gas Corporation Ltd.", "AAA"),
        ("GAIL", "GAIL (India) Ltd.", "AAA"), ("NLC", "NLC India Ltd.", "AAA")]

# --------------------------------------------------------------------------- IPOs
REAL_IPOS = """FSN E-Commerce Ventures|One 97 Communications|PB Fintech|CarTrade Tech|Nuvoco Vistas Corporation|Star Health and Allied Insurance|Sapphire Foods India|Aptus Value Housing Finance India|Tega Industries|Fino Payments Bank|Go Fashion (India)|Metro Brands|Tatva Chintan Pharma Chem|Supriya Lifescience|Latent View Analytics|SJS Enterprises|RateGain Travel Technologies|Data Patterns (India)|Ami Organics|Easy Trip Planners|Chemplast Sanmar|Nazara Technologies|MTAR Technologies|Anand Rathi Wealth|MedPlus Health Services|Vijaya Diagnostic Centre|Clean Science and Technology|Devyani International|Krsnaa Diagnostics|Windlas Biotech|Life Insurance Corporation of India|Delhivery|Venus Pipes & Tubes|Campus Activewear|Syrma SGS Technology|DreamFolks Services|Tracxn Technologies|Fusion Micro Finance|Ethos Ltd|Rainbow Children's Medicare|Harsha Engineers International|Prudent Corporate Advisory Services|Vedant Fashions|Aether Industries|Global Health|AGS Transact Technologies|Five-Star Business Finance|Keystone Realtors|Uniparts India|KFin Technologies|Abans Holdings|Sula Vineyards|Bikaji Foods International|Mankind Pharma|Cyient DLM|IKIO Lighting|Netweb Technologies India|Honasa Consumer|Utkarsh Small Finance Bank|Avalon Technologies|Senco Gold|ideaForge Technology|SBFC Finance|Concord Biotech|JSW Infrastructure|Aeroflex Industries|Pyramid Technoplast|Rishabh Instruments|Zaggle Prepaid Ocean Services|Tata Technologies|Indian Renewable Energy Development Agency|Gandhar Oil Refinery (India)|Flair Writing Industries|Blue Jet Healthcare|Protean eGov Technologies|Fedbank Financial Services|Doms Industries|Suraj Estate Developers|Happy Forgings|Inox India|Updater Services|RR Kabel|Kaynes Technology India|Hyundai Motor India|Bajaj Housing Finance|NTPC Green Energy|Ola Electric Mobility|Vishal Mega Mart|Waaree Energies|Premier Energies|Brainbees Solutions|Awfis Space Solutions|Le Travenues Technology|Go Digit General Insurance|Bharti Hexacom|Popular Vehicles and Services|Indegene|Aadhar Housing Finance|Ceigall India|Emcure Pharmaceuticals|TBO Tek|Allied Blenders and Distillers|Sagility India|Stanley Lifestyles|Mukka Proteins|Entero Healthcare Solutions|Exicom Tele-Systems|Ather Energy|HDB Financial Services|Tata Capital|Anthem Biosciences|Hexaware Technologies|Travel Food Services|Ellenbarrie Industrial Gases""".split("|")
PIPELINE = [  # fictional names on purpose: these carry "live" statuses, which must not be pinned on real firms
    ("Sample Renewables Ltd", "OPEN"), ("Demo Logistics Tech Ltd", "OPEN"), ("Example Fintech Ltd", "OPEN"),
    ("Placeholder Pharma Ltd", "OPEN"), ("Test Retail Ventures Ltd", "UPCOMING"),
    ("Mock Semiconductors Ltd", "UPCOMING"), ("Sandbox Hospitals Ltd", "UPCOMING"),
    ("Dummy Foods Ltd", "UPCOMING"), ("Trial Insurance Brokers Ltd", "UPCOMING"),
    ("Sample Defence Systems Ltd", "UPCOMING"), ("Demo Housing Finance Ltd", "CLOSED"),
    ("Example Textiles Ltd", "CLOSED"), ("Placeholder Mobility Ltd", "CLOSED"),
    ("Test Chemicals Ltd", "CLOSED")]


def build():
    inst = []     # dicts: symbol,name,exchange,currency,lot,tick,asset_class,sector,tradable,underlying
    params = []   # symbol, base, amp, start_date, vol_lo, vol_span
    mf, bonds, ipos, members = [], [], [], []

    # equities
    for sym, name, sector, w in NIFTY:
        inst.append(dict(symbol=sym, name=name, exchange="NSE", currency="INR", lot=1, tick=0.05,
                         asset_class="EQUITY", sector=sector, tradable=True, underlying=None))
        params.append((sym, 150 + h(sym) % 5800, 0.04, "2026-07-20", 800000, 2000000))
        members.append(("NIFTY", sym, w))
    for sym, name, sector in BANK_ONLY:
        inst.append(dict(symbol=sym, name=name, exchange="NSE", currency="INR", lot=1, tick=0.05,
                         asset_class="EQUITY", sector=sector, tradable=True, underlying=None))
        params.append((sym, 30 + h(sym) % 1500, 0.045, "2026-07-20", 1500000, 5000000))
    for sym, w in BANKNIFTY_W.items():
        members.append(("BANKNIFTY", sym, w))
    for sym, w in SENSEX_W.items():
        members.append(("SENSEX", sym, w))

    # indices + GIFT Nifty
    for sym, name, ex, cur, tick, base, amp in INDICES:
        inst.append(dict(symbol=sym, name=name, exchange=ex, currency=cur, lot=1, tick=tick,
                         asset_class="INDEX", sector=None, tradable=False, underlying=None))
        params.append((sym, base, amp, "2026-07-20", 0, 1))
    sym, name, ex, cur, tick, base, amp = GIFT
    inst.append(dict(symbol=sym, name=name, exchange=ex, currency=cur, lot=1, tick=tick,
                     asset_class="INDEX_FUTURE", sector=None, tradable=True, underlying="NIFTY"))
    # GIFT Nifty prices are derived from the Nifty series (see seed SQL), so no params row

    # mutual funds (15 AMCs x 8 categories = 120)
    for amc in AMCS:
        for cat, grp, er, lo, span, amp in MF_CATS:
            sym = "MF-" + "".join(c for c in amc.upper() if c.isalnum())[:8] + "-" + \
                  "".join(c for c in cat.upper() if c.isalnum())[:8]
            name = f"{amc} {cat} Fund - Direct Growth"
            inst.append(dict(symbol=sym, name=name, exchange="AMFI", currency="INR", lot=1, tick=0.0001,
                             asset_class="MUTUAL_FUND", sector=None, tradable=True, underlying=None))
            params.append((sym, lo + h(sym) % span, amp, "2026-07-20", 0, 1))
            mf.append((sym, amc, cat, grp, er, 500 if grp != "Debt" else 1000))

    # bonds: 24 G-secs + 20 SDLs + 4 T-bills + 56 corporates = 104
    cps = [6.54, 6.79, 7.10, 7.18, 7.26, 7.30, 7.34, 6.92]
    for i in range(24):
        yr, cp = 2028 + i, cps[i % len(cps)]
        sym = f"GS{yr}-{int(cp * 100)}"
        bonds.append((sym, f"{cp:.2f}% GOI {yr}", "GOI", "GSEC", cp, 100, f"{yr}-{1 + i % 12:02d}-15", 2, "SOVEREIGN", 100))
    for i, (code, st) in enumerate(STATES):
        yr, cp = 2030 + i % 12, 7.20 + (h(code) % 40) / 100
        sym = f"SDL-{code}-{yr}"
        bonds.append((sym, f"{cp:.2f}% {st} SDL {yr}", st, "SDL", cp, 100, f"{yr}-{1 + i % 12:02d}-20", 2, "SOVEREIGN", 100))
    for sym, tenor, i in [("TB91-A", "91-Day", 0), ("TB91-B", "91-Day", 1), ("TB182", "182-Day", 2), ("TB364", "364-Day", 3)]:
        bonds.append((sym, f"{tenor} Treasury Bill", "GOI", "TBILL", 0, 100, f"2027-{1 + i * 3:02d}-10", 0, "SOVEREIGN", 100))
    for i, (code, issuer, rating) in enumerate(CORP):
        for k, yr in enumerate((2028 + h(code) % 3, 2032 + h(code) % 4)):
            cp = round(7.30 + (h(code + str(k)) % 220) / 100, 2)
            sym = f"{code}-{yr}"
            bonds.append((sym, f"{issuer} {cp:.2f}% NCD {yr}", issuer, "CORPORATE", cp, 1000,
                          f"{yr}-{1 + (i + k * 5) % 12:02d}-25", 1, rating, 1000))
    for b in bonds:
        sym, name, issuer, typ, cp, face = b[0], b[1], b[2], b[3], b[4], b[5]
        inst.append(dict(symbol=sym, name=name, exchange="NSE", currency="INR", lot=1, tick=0.01,
                         asset_class="BOND", sector=None, tradable=True, underlying=None))
        base = face * (0.98 if typ == "TBILL" else 0.96 + (h(sym) % 80) / 1000)
        params.append((sym, round(base, 2), 0.004 if typ != "TBILL" else 0.0005, "2026-07-20", 5000, 60000))

    # IPOs: real names are LISTED (synthetic numbers, no dates); fictional names carry live statuses
    entries = [(n, "LISTED") for n in REAL_IPOS] + PIPELINE
    for n, status in entries:
        code = "".join(c for c in n.upper() if c.isalnum())[:12]
        sym = f"IPO-{code}"
        n_dupe = sum(1 for e in inst if e["symbol"].startswith(sym))
        if n_dupe:
            sym += str(n_dupe)
        lot = [7, 10, 12, 15, 20, 25, 30, 40, 50, 60][h(sym) % 10]
        hi = 60 + h(sym) % 900
        lo = round(hi * 0.95)
        inst.append(dict(symbol=sym, name=f"{n} IPO", exchange="NSE", currency="INR", lot=lot, tick=0.05,
                         asset_class="IPO", sector=None, tradable=True, underlying=None))
        if status == "LISTED":
            params.append((sym, round(hi * (1 + (h(sym + "x") % 90) / 100), 2), 0.05, "2026-07-20", 400000, 3000000))
            ipos.append((sym, n, status, None, None, None, lo, hi, hi, lot, round(2 + (h(sym) % 900) / 10, 2)))
        else:
            o = {"OPEN": ("2026-10-22", "2026-10-27", None, 12),
                 "UPCOMING": ("2026-10-{}".format(28 + h(sym) % 3), "2026-11-0{}".format(2 + h(sym) % 4), None, None),
                 "CLOSED": ("2026-10-14", "2026-10-16", "2026-10-23", 40)}[status]
            ipos.append((sym, n, status, o[0], o[1], o[2], lo, hi, None, lot,
                         None if status == "UPCOMING" else round(1 + (h(sym) % 600) / 10, 2)))
    return inst, params, mf, bonds, ipos, members


EXT = """-- Market-universe extension (run after 01-03 schema files, before 04_seed.sql).
-- Safe to re-run. Adds asset classes (equity, index, index future, mutual fund, bond, IPO) to
-- `instrument`, plus per-class detail tables and index membership.
SET search_path = public;

ALTER TABLE instrument ADD COLUMN IF NOT EXISTS asset_class text NOT NULL DEFAULT 'EQUITY';
ALTER TABLE instrument ADD COLUMN IF NOT EXISTS name text;
ALTER TABLE instrument ADD COLUMN IF NOT EXISTS sector text;
ALTER TABLE instrument ADD COLUMN IF NOT EXISTS tradable boolean NOT NULL DEFAULT true;
ALTER TABLE instrument ADD COLUMN IF NOT EXISTS underlying_id bigint;      -- e.g. GIFT Nifty -> Nifty 50
ALTER TABLE instrument ADD COLUMN IF NOT EXISTS feed_symbol text;          -- provider-side ticker for live prices
ALTER TABLE instrument ADD COLUMN IF NOT EXISTS data_source text NOT NULL DEFAULT 'seed-synthetic';

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'instrument_asset_class_chk') THEN
    ALTER TABLE instrument ADD CONSTRAINT instrument_asset_class_chk
      CHECK (asset_class IN ('EQUITY','INDEX','INDEX_FUTURE','MUTUAL_FUND','BOND','IPO'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS instrument_asset_class_idx ON instrument (asset_class);

-- index constituents (Nifty 50, Nifty Bank, Sensex)
CREATE TABLE IF NOT EXISTS index_membership (
  index_id   bigint NOT NULL,
  member_id  bigint NOT NULL,
  weight_pct numeric(6,3),
  as_of      date NOT NULL DEFAULT date '2026-10-24',
  PRIMARY KEY (index_id, member_id)
);

CREATE TABLE IF NOT EXISTS mf_scheme (
  instrument_id bigint PRIMARY KEY,
  amc           text NOT NULL,
  category      text NOT NULL,
  scheme_group  text NOT NULL CHECK (scheme_group IN ('Equity','Debt','Index')),
  expense_ratio numeric(5,2),
  min_investment numeric(12,2)
);

CREATE TABLE IF NOT EXISTS bond (
  instrument_id bigint PRIMARY KEY,
  issuer        text NOT NULL,
  bond_type     text NOT NULL CHECK (bond_type IN ('GSEC','SDL','TBILL','CORPORATE')),
  coupon_pct    numeric(6,3) NOT NULL,
  face_value    numeric(12,2) NOT NULL,
  maturity_date date NOT NULL,
  coupons_per_year smallint NOT NULL,
  rating        text
);

CREATE TABLE IF NOT EXISTS ipo (
  instrument_id bigint PRIMARY KEY,
  company       text NOT NULL,
  status        text NOT NULL CHECK (status IN ('UPCOMING','OPEN','CLOSED','LISTED')),
  issue_open    date,
  issue_close   date,
  allotment_date date,
  price_band_low  numeric(12,2),
  price_band_high numeric(12,2),
  issue_price   numeric(12,2),
  lot_size      integer NOT NULL,
  subscription_x numeric(8,2)
);

-- live price feed bookkeeping (populated by the price-updater service; see price-feed discussion)
CREATE TABLE IF NOT EXISTS price_feed_state (
  source          text PRIMARY KEY,
  last_success_at timestamptz,
  last_error_at   timestamptz,
  last_error      text
);
"""


def main():
    inst, params, mf, bonds, ipos, members = build()
    counts = {}
    for i in inst:
        counts[i["asset_class"]] = counts.get(i["asset_class"], 0) + 1
    assert counts["MUTUAL_FUND"] >= 100 and counts["BOND"] >= 100 and counts["IPO"] >= 100, counts
    syms = [i["symbol"] for i in inst]
    assert len(syms) == len(set(syms)), "duplicate symbols"
    assert sum(1 for m in members if m[0] == "NIFTY") == 50
    assert sum(1 for m in members if m[0] == "BANKNIFTY") == 14
    assert sum(1 for m in members if m[0] == "SENSEX") == 30

    (OUT / "03b_market_extension.sql").write_text(EXT)

    L = []
    L.append("""-- Demo data (report 10.4): Indian market universe. Deterministic, so tests and the demo are repeatable.
--   59 equities (all Nifty 50 + Nifty Bank + Sensex members), 3 indices (Nifty 50, Nifty Bank, Sensex),
--   GIFT Nifty, 120 mutual funds, 104 bonds, 128 IPOs; 5 users (1 admin, 1 auditor); ~3 months of prices.
-- Index membership: public sources, checked 24 Oct 2026 (Nifty 50 reflects BSE Ltd. replacing Wipro on
-- 30 Sep 2026). Prices, NAVs, coupons and IPO terms are SYNTHETIC placeholders (data_source='seed-synthetic');
-- the live price feed overwrites latest_price.
-- Seed users carry a placeholder password hash; run api/seed_passwords.py to set real Argon2id
-- hashes before logging in through the API.
-- Requires 03b_market_extension.sql. Run once on a freshly created database.

SET search_path = public;

-- monthly partitions for price_snapshot (Jul-Dec 2026)
DO $$
DECLARE m date;
BEGIN
  FOR m IN SELECT generate_series(date '2026-07-01', date '2026-12-01', interval '1 month')::date LOOP
    EXECUTE format('CREATE TABLE IF NOT EXISTS price_snapshot_%s PARTITION OF price_snapshot
                    FOR VALUES FROM (%L) TO (%L)',
                   to_char(m, 'YYYY_MM'), m, (m + interval '1 month')::date);
  END LOOP;
END $$;

-- demo brokerage fees (flat placeholders, not real NSE/AMFI charges)
INSERT INTO fee_schedule (exchange, pct, min_fee) VALUES
 ('NSE', 0.0003, 5.00), ('BSE', 0.0003, 5.00), ('NSE_IX', 0.0002, 1.00), ('AMFI', 0, 0);
""")
    L.append("INSERT INTO instrument (symbol, exchange, currency, lot_size, tick_size, asset_class, name, sector, tradable) VALUES")
    L.append(",\n".join(
        f" ({q(i['symbol'])},{q(i['exchange'])},{q(i['currency'])},{i['lot']},{i['tick']},{q(i['asset_class'])},"
        f"{q(i['name'])},{q(i['sector'])},{'true' if i['tradable'] else 'false'})" for i in inst) + ";\n")
    L.append("UPDATE instrument g SET underlying_id = n.instrument_id FROM instrument n "
             "WHERE g.symbol = 'GIFTNIFTY' AND n.symbol = 'NIFTY';\n")

    L.append("INSERT INTO index_membership (index_id, member_id, weight_pct)\nSELECT ix.instrument_id, m.instrument_id, v.w\nFROM (VALUES")
    L.append(",\n".join(f" ({q(a)},{q(b)},{'NULL::numeric' if w is None else w})" for a, b, w in members))
    L.append(") AS v(idx, sym, w)\nJOIN instrument ix ON ix.symbol = v.idx\nJOIN instrument m ON m.symbol = v.sym;\n")

    L.append("INSERT INTO mf_scheme (instrument_id, amc, category, scheme_group, expense_ratio, min_investment)\nSELECT i.instrument_id, v.amc, v.cat, v.grp, v.er, v.minv FROM (VALUES")
    L.append(",\n".join(f" ({q(s)},{q(a)},{q(c)},{q(g)},{e},{m})" for s, a, c, g, e, m in mf))
    L.append(") AS v(sym, amc, cat, grp, er, minv) JOIN instrument i ON i.symbol = v.sym;\n")

    L.append("INSERT INTO bond (instrument_id, issuer, bond_type, coupon_pct, face_value, maturity_date, coupons_per_year, rating)\nSELECT i.instrument_id, v.issuer, v.typ, v.cp, v.face, v.mat::date, v.cpy, v.rating FROM (VALUES")
    L.append(",\n".join(f" ({q(b[0])},{q(b[2])},{q(b[3])},{b[4]},{b[5]},{q(b[6])},{b[7]},{q(b[8])})" for b in bonds))
    L.append(") AS v(sym, issuer, typ, cp, face, mat, cpy, rating) JOIN instrument i ON i.symbol = v.sym;\n")

    def d(x):
        return "NULL::date" if x is None else f"'{x}'::date"

    def n(x):
        return "NULL::numeric" if x is None else str(x)

    L.append("INSERT INTO ipo (instrument_id, company, status, issue_open, issue_close, allotment_date, price_band_low, price_band_high, issue_price, lot_size, subscription_x)\nSELECT i.instrument_id, v.company, v.status, v.o, v.c, v.a, v.lo, v.hi, v.ip, v.lot, v.sub FROM (VALUES")
    L.append(",\n".join(f" ({q(s)},{q(co)},{q(st)},{d(o)},{d(c)},{d(a)},{n(lo)},{n(hi)},{n(ip)},{lot},{n(sub)})"
                        for s, co, st, o, c, a, lo, hi, ip, lot, sub in ipos))
    L.append(") AS v(sym, company, status, o, c, a, lo, hi, ip, lot, sub) JOIN instrument i ON i.symbol = v.sym;\n")

    L.append("-- price model parameters per instrument (base level, wave amplitude, volume range)\nCREATE TEMP TABLE seed_params (symbol text, base numeric, amp numeric, start_date date, vol_lo int, vol_span int);\nINSERT INTO seed_params VALUES")
    L.append(",\n".join(f" ({q(s)},{b},{a},'{sd}',{vl},{vs})" for s, b, a, sd, vl, vs in params) + ";\n")

    L.append("""-- weekday prices (15:30 IST close = 10:00 UTC): smooth wave + hash-based noise, so reruns are identical
INSERT INTO price_snapshot (instrument_id, ts, open, high, low, close, volume, source)
SELECT i.instrument_id, d.ts,
       p.base * 0.998, p.base * 1.010, p.base * 0.990, p.base,
       sp.vol_lo + (abs(hashtext(i.symbol || d.ts::text)) % sp.vol_span), 'seed'
FROM instrument i
JOIN seed_params sp ON sp.symbol = i.symbol
CROSS JOIN LATERAL generate_series(timestamptz '2026-07-20 10:00+00',
                                   timestamptz '2026-10-23 10:00+00', interval '1 day') AS d(ts)
CROSS JOIN LATERAL (SELECT round(
     sp.base * (1 + sp.amp * sin(extract(doy FROM d.ts) / 6.0 + (abs(hashtext(i.symbol)) % 7))
          + ((abs(hashtext(i.symbol || d.ts::text)) % 1000) / 1000.0 - 0.5) * sp.amp / 4)::numeric, 2) AS base) p
WHERE extract(isodow FROM d.ts) < 6
  AND d.ts::date >= sp.start_date;

-- GIFT Nifty tracks Nifty 50 with a small deterministic basis (+0.05% .. +0.25%)
INSERT INTO price_snapshot (instrument_id, ts, open, high, low, close, volume, source)
SELECT g.instrument_id, ps.ts,
       round(ps.open  * (1 + b.basis), 2), round(ps.high * (1 + b.basis), 2),
       round(ps.low   * (1 + b.basis), 2), round(ps.close * (1 + b.basis), 2),
       50000 + (abs(hashtext('GIFTNIFTY' || ps.ts::text)) % 400000), 'seed'
FROM instrument g
JOIN instrument n ON n.symbol = 'NIFTY'
JOIN price_snapshot ps ON ps.instrument_id = n.instrument_id
CROSS JOIN LATERAL (SELECT 0.0005 + (abs(hashtext('GIFTNIFTY' || ps.ts::text)) % 200) / 100000.0 AS basis) b
WHERE g.symbol = 'GIFTNIFTY';

INSERT INTO latest_price (instrument_id, price, ts)
SELECT DISTINCT ON (instrument_id) instrument_id, close, ts
FROM price_snapshot ORDER BY instrument_id, ts DESC;

-- IPOs without trading history yet: show the upper price band
INSERT INTO latest_price (instrument_id, price, ts)
SELECT o.instrument_id, o.price_band_high, timestamptz '2026-10-23 10:00+00'
FROM ipo o WHERE NOT EXISTS (SELECT 1 FROM latest_price lp WHERE lp.instrument_id = o.instrument_id);

-- users: 1 admin, 1 auditor, 3 traders
INSERT INTO app_user (email, password_hash) VALUES
 ('admin@example.test',   'SEED-SET-WITH-seed_passwords.py'),
 ('auditor@example.test', 'SEED-SET-WITH-seed_passwords.py'),
 ('alice@example.test',   'SEED-SET-WITH-seed_passwords.py'),
 ('bob@example.test',     'SEED-SET-WITH-seed_passwords.py'),
 ('carol@example.test',   'SEED-SET-WITH-seed_passwords.py');
INSERT INTO user_role (user_id, role_id)
SELECT u.user_id, r.role_id FROM app_user u JOIN role r ON r.name =
  CASE u.email WHEN 'admin@example.test' THEN 'ADMIN' WHEN 'auditor@example.test' THEN 'AUDITOR' ELSE 'USER' END;
INSERT INTO portfolio (user_id, name)
SELECT user_id, 'Main' FROM app_user WHERE email IN ('alice@example.test','bob@example.test','carol@example.test');
""")
    (OUT / "04_seed.sql").write_text("\n".join(L))
    print(counts, "total", len(inst))


main()
