from flask import Flask, jsonify, render_template
import requests
from datetime import datetime
import pytz
import re
import os

app = Flask(__name__)

ISTANBUL_TZ = pytz.timezone("Europe/Istanbul")

BESIKTAS_LAT = 41.0429
BESIKTAS_LON = 29.0061

ISKI_API_BASE = "https://iskiapi.iski.istanbul/api/"
_ISKI_TOKEN_CACHE = {"token": None}

_ISKI_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://iski.istanbul/"}


def _get_iski_token(force_refresh=False):
    """Fetch the ISKI auth token by scanning Nuxt bundle scripts dynamically."""
    if _ISKI_TOKEN_CACHE["token"] and not force_refresh:
        return _ISKI_TOKEN_CACHE["token"]

    # Step 1: fetch the homepage and extract /_nuxt/*.js script URLs
    home_status = None
    try:
        home = requests.get("https://iski.istanbul/", headers=_ISKI_HEADERS, timeout=15)
        home_status = home.status_code
        script_urls = re.findall(r'/_nuxt/[^"\']+\.js', home.text)
        seen = set()
        unique_scripts = [u for u in script_urls if not (u in seen or seen.add(u))]
    except Exception as e:
        _ISKI_TOKEN_CACHE["debug"] = f"Homepage fetch failed: {e}"
        return None

    if not unique_scripts:
        _ISKI_TOKEN_CACHE["debug"] = f"Homepage {home_status}: no /_nuxt/*.js scripts found"
        return None

    # Step 2: scan each script for the auth token (try multiple patterns)
    token_patterns = [
        r'NUXT_ENV_AUTH_TOKEN:"([^"]+)"',
        r'"NUXT_ENV_AUTH_TOKEN"\s*:\s*"([^"]+)"',
        r"NUXT_ENV_AUTH_TOKEN:'([^']+)'",
    ]
    for path in unique_scripts:
        try:
            r = requests.get(
                f"https://iski.istanbul{path}",
                headers=_ISKI_HEADERS,
                timeout=15,
            )
            for pattern in token_patterns:
                match = re.search(pattern, r.text)
                if match:
                    _ISKI_TOKEN_CACHE["token"] = match.group(1)
                    _ISKI_TOKEN_CACHE["debug"] = None
                    return _ISKI_TOKEN_CACHE["token"]
        except Exception:
            continue

    _ISKI_TOKEN_CACHE["debug"] = f"Scanned {len(unique_scripts)} scripts, token not found"
    return None


IBB_DAM_URL = (
    "https://data.ibb.gov.tr/api/3/action/datastore_search_sql"
    '?sql=SELECT%20*%20from%20%22b68cbdb0-9bf5-474c-91c4-9256c07c4bdf%22'
    "%20ORDER%20BY%20%22TARIH%22%20DESC%20LIMIT%2020"
)


def fetch_dam_data():
    """Fetch Istanbul dam fill rates.

    Tries ISKI live API first; falls back to IBB open-data (CKAN) if
    iski.istanbul is unreachable (e.g. foreign server IP blocked).
    """
    # --- Primary: ISKI live API ---
    token = _get_iski_token()
    if token:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Referer": "https://iski.istanbul/",
        }
        try:
            r1 = requests.get(ISKI_API_BASE + "iski/baraj/gunlukOzet/v2", headers=headers, timeout=15)
            if r1.status_code == 401:
                token = _get_iski_token(force_refresh=True)
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    r1 = requests.get(ISKI_API_BASE + "iski/baraj/gunlukOzet/v2", headers=headers, timeout=15)
            r1.raise_for_status()
            summary = r1.json()

            r2 = requests.get(ISKI_API_BASE + "iski/baraj/genelOran/v2", headers=headers, timeout=15)
            r2.raise_for_status()
            general = r2.json()

            dams = [
                {"name": item["baslikAdi"], "rate": float(item["yuzde"]),
                 "m3": item.get("m3"), "kita": item.get("kita")}
                for item in summary.get("data", [])
            ]
            overall = general.get("data", {}).get("oran")
            updated = summary.get("sonGuncellemeZamani", "")
            return {"dams": dams, "overall": overall, "updated": updated, "source": "iski.istanbul", "error": None}
        except Exception:
            pass  # fall through to IBB

    # --- Fallback: IBB open-data (CKAN) ---
    try:
        r = requests.get(IBB_DAM_URL, timeout=15)
        r.raise_for_status()
        records = r.json().get("result", {}).get("records", [])
        if records:
            latest_date = records[0]["TARIH"]
            latest = [rec for rec in records if rec["TARIH"] == latest_date]
            dams = [{"name": rec["BARAJ_ADI"], "rate": float(rec["DOLULUK_ORANI"])} for rec in latest]
            overall = round(sum(d["rate"] for d in dams) / len(dams), 1) if dams else None
            return {"dams": dams, "overall": overall, "updated": latest_date, "source": "data.ibb.gov.tr", "error": None}
    except Exception:
        pass

    return {"dams": [], "overall": None, "source": "-", "error": "Baraj verisi alınamadı (her iki kaynak da erişilemiyor)"}


def fetch_weather():
    """Fetch next 12 hours hourly weather for Besiktas via Open-Meteo."""
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={BESIKTAS_LAT}&longitude={BESIKTAS_LON}"
        f"&hourly=temperature_2m,precipitation_probability,weather_code"
        f"&forecast_days=2"
        f"&timezone=Europe%2FIstanbul"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        now = datetime.now(ISTANBUL_TZ)
        hourly = data["hourly"]

        result = []
        count = 0
        for i, time_str in enumerate(hourly["time"]):
            dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M")
            dt_local = ISTANBUL_TZ.localize(dt)
            if dt_local >= now and count < 12:
                result.append({
                    "time": dt_local.strftime("%H:%M"),
                    "date": dt_local.strftime("%d %b"),
                    "temp": hourly["temperature_2m"][i],
                    "precip_prob": hourly["precipitation_probability"][i],
                    "weather_code": hourly["weather_code"][i],
                })
                count += 1

        return {"hours": result, "error": None}
    except Exception as e:
        return {"hours": [], "error": str(e)}


@app.route("/")
def index():
    return render_template("index.html")


ASSETS = [
    {"symbol": "CME_MINI:NQ1!", "label": "NQ1!",    "desc": "Nasdaq 100"},
    {"symbol": "CME_MINI:ES1!", "label": "ES1!",    "desc": "S&P 500"},
    {"symbol": "CME_MINI:RTY1!","label": "RTY1!",   "desc": "Russell 2000"},
    {"symbol": "COMEX:SI1!",    "label": "SI1!",    "desc": "Gümüş"},
    {"symbol": "OANDA:XAUUSD",  "label": "XAUUSD",  "desc": "Altın"},
    {"symbol": "BITSTAMP:BTCUSD","label": "BTCUSD", "desc": "Bitcoin"},
    {"symbol": "BITSTAMP:ETHUSD","label": "ETHUSD", "desc": "Ethereum"},
    {"symbol": "NYMEX:CL1!",    "label": "WTI",     "desc": "Ham Petrol"},
]

TV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}


def fetch_markets():
    """Fetch asset prices and daily change from TradingView scanner API."""
    tickers = [a["symbol"] for a in ASSETS]
    payload = {
        "symbols": {"tickers": tickers, "query": {"types": []}},
        "columns": ["close", "change", "change_abs"],
    }
    try:
        r = requests.post(
            "https://scanner.tradingview.com/global/scan",
            json=payload,
            headers=TV_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        data_map = {item["s"]: item["d"] for item in r.json().get("data", [])}

        result = []
        for asset in ASSETS:
            d = data_map.get(asset["symbol"])
            if d:
                result.append({
                    "label": asset["label"],
                    "desc": asset["desc"],
                    "price": d[0],
                    "change_pct": round(d[1], 2),
                    "change_abs": round(d[2], 2),
                })
        return {"assets": result, "error": None}
    except Exception as e:
        return {"assets": [], "error": str(e)}


@app.route("/api/data")
def api_data():
    dam_data = fetch_dam_data()
    weather_data = fetch_weather()
    market_data = fetch_markets()
    return jsonify({
        "dams": dam_data,
        "weather": weather_data,
        "markets": market_data,
        "fetched_at": datetime.now(ISTANBUL_TZ).strftime("%d %b %Y %H:%M"),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
