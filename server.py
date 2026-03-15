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


def _get_iski_token():
    """Fetch the ISKI auth token from the Nuxt bundle (cached per process)."""
    if _ISKI_TOKEN_CACHE["token"]:
        return _ISKI_TOKEN_CACHE["token"]
    try:
        r = requests.get(
            "https://iski.istanbul/_nuxt/57631b9.js",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://iski.istanbul/"},
            timeout=15,
        )
        match = re.search(r'NUXT_ENV_AUTH_TOKEN:"([^"]+)"', r.text)
        if match:
            _ISKI_TOKEN_CACHE["token"] = match.group(1)
            return _ISKI_TOKEN_CACHE["token"]
    except Exception:
        pass
    return None


def fetch_dam_data():
    """Fetch Istanbul dam fill rates from ISKI API."""
    token = _get_iski_token()
    if not token:
        return {"dams": [], "overall": None, "source": "iski.istanbul", "error": "Token alınamadı"}

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Referer": "https://iski.istanbul/",
    }
    try:
        # Daily summary per dam
        r1 = requests.get(ISKI_API_BASE + "iski/baraj/gunlukOzet/v2", headers=headers, timeout=15)
        r1.raise_for_status()
        summary = r1.json()

        # Overall fill rate
        r2 = requests.get(ISKI_API_BASE + "iski/baraj/genelOran/v2", headers=headers, timeout=15)
        r2.raise_for_status()
        general = r2.json()

        dams = [
            {
                "name": item["baslikAdi"],
                "rate": float(item["yuzde"]),
                "m3": item.get("m3"),
                "kita": item.get("kita"),
            }
            for item in summary.get("data", [])
        ]
        overall = general.get("data", {}).get("oran")
        updated = summary.get("sonGuncellemeZamani", "")

        return {"dams": dams, "overall": overall, "updated": updated, "source": "iski.istanbul", "error": None}
    except Exception as e:
        return {"dams": [], "overall": None, "source": "iski.istanbul", "error": str(e)}


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
