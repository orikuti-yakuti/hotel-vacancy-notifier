#!/usr/bin/env python3
"""指定エリアのホテルの空室を楽天トラベル空室検索APIで確認し、
条件に合うホテルに新しく空きが出たらLINEに通知する。

- 監視条件は watches.toml に書く
- 認証情報は環境変数（GitHub Actions の Secrets）から読む
    RAKUTEN_APP_ID, RAKUTEN_ACCESS_KEY, LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID
- 前回の結果を state/state.json に保存し、「前回は条件に合う空室がなかったホテル」に
  空室が出たときだけ通知する（同じホテルで何度も通知しない）

使い方:
    python check_vacancy.py               # 監視を1回実行
    python check_vacancy.py --test-line   # LINEにテスト通知だけ送る
    python check_vacancy.py --dry-run     # LINEに送らず、条件に合うホテルを一覧表示
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "watches.toml"
STATE_PATH = ROOT / "state" / "state.json"

RAKUTEN_VACANT_URL = (
    "https://openapi.rakuten.co.jp/engine/api/Travel/VacantHotelSearch/20170426"
)
STATION_API_URL = "https://express.heartrails.com/api/json"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
JST = dt.timezone(dt.timedelta(hours=9))
USER_AGENT = "hotel-vacancy-notifier/2.0 (personal use)"

MAX_PAGES_PER_SEARCH = 5      # 1エリアあたり最大 5ページ × 30プラン
MAX_HOTELS_IN_MESSAGE = 10    # 1通に載せるホテル数の上限
API_INTERVAL_SEC = 1.5        # 楽天APIへの連続アクセスの間隔（短いと429になる）

# カプセルホテル・相部屋など「鍵付きの個室ではない」宿を見分けるための言葉
CAPSULE_KEYWORDS = [
    "カプセル", "キャビン", "ドミトリー", "相部屋", "ゲストハウス", "ホステル",
    "シェアルーム", "二段ベッド", "2段ベッド", "ネットカフェ", "漫画喫茶", "マンガ喫茶",
    "capsule", "cabin", "dormitory", "dorm", "hostel", "guest house", "guesthouse",
]

# 部屋タイプの判定に使う言葉（部屋名・プラン名に含まれるかで判定）
ROOM_TYPE_KEYWORDS = {
    "シングル": ["シングル", "single"],
    "セミダブル": ["セミダブル", "semi-double", "semidouble", "semi double"],
    "ダブル": ["ダブル", "double"],
    "ツイン": ["ツイン", "twin"],
    "トリプル": ["トリプル", "triple"],
}
SMOKING_CHOICES = {"禁煙", "喫煙", "どちらでも"}


class ConfigError(Exception):
    """設定や認証の誤り。直さない限り何度実行しても失敗するもの。"""


# ================================================================ 設定まわり


def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise ConfigError(f"環境変数 {name} が設定されていません（GitHub の Secrets を確認してください）")
    return value


def _as_list(value, label: str, key: str) -> list:
    if value is None:
        return []
    if isinstance(value, (str, int)):
        return [value]
    if isinstance(value, list):
        return value
    raise ConfigError(f"watches.toml の「{label}」の {key} は [\"…\", \"…\"] の形で書いてください")


def _as_price(value, label: str, key: str):
    if value in (None, "", 0):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"watches.toml の「{label}」の {key} は数字で書いてください（例: 8000）") from e


def load_watches() -> list[dict]:
    if not CONFIG_PATH.exists():
        raise ConfigError(f"{CONFIG_PATH.name} が見つかりません")
    try:
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"watches.toml の書き方に誤りがあります: {e}\n"
            "（引用符が全角になっていないか、[ ] や \" の閉じ忘れがないか確認してください）"
        ) from e

    watches = data.get("watch", [])
    if not watches:
        raise ConfigError("watches.toml に [[watch]] が1つもありません")

    normalized = []
    for i, w in enumerate(watches, start=1):
        label = w.get("name") or f"{i}件目"
        try:
            checkin = dt.date.fromisoformat(str(w["checkin"]))
            checkout = dt.date.fromisoformat(str(w["checkout"]))
        except KeyError as e:
            raise ConfigError(f"watches.toml の「{label}」に {e.args[0]} がありません") from e
        except ValueError as e:
            raise ConfigError(f"watches.toml の「{label}」の日付は \"2026-12-20\" の形で書いてください") from e
        if checkout <= checkin:
            raise ConfigError(f"watches.toml の「{label}」: checkout は checkin より後の日付にしてください")

        stations = [str(s).strip() for s in _as_list(w.get("stations"), label, "stations") if str(s).strip()]
        hotels = []
        for h in _as_list(w.get("hotels"), label, "hotels"):
            try:
                hotels.append(int(h))
            except (TypeError, ValueError) as e:
                raise ConfigError(f"watches.toml の「{label}」の hotels には楽天の施設番号（数字）を書いてください") from e
        if not stations and not hotels:
            raise ConfigError(
                f"watches.toml の「{label}」: stations（駅名）か hotels（施設番号）のどちらかを書いてください"
            )
        if len(hotels) > 15:
            raise ConfigError(f"watches.toml の「{label}」: hotels は1つの監視につき15件までです")

        try:
            radius = round(float(w.get("radius_km", 1.0)), 1)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"watches.toml の「{label}」の radius_km は数字で書いてください（例: 1.0）") from e
        if not 0.1 <= radius <= 3.0:
            raise ConfigError(f"watches.toml の「{label}」の radius_km は 0.1〜3.0 の範囲で書いてください")

        min_price = _as_price(w.get("min_price"), label, "min_price")
        max_price = _as_price(w.get("max_price"), label, "max_price")
        if min_price and max_price and min_price > max_price:
            raise ConfigError(f"watches.toml の「{label}」: min_price が max_price より大きくなっています")

        smoking = str(w.get("smoking", "どちらでも")).strip()
        if smoking not in SMOKING_CHOICES:
            raise ConfigError(
                f"watches.toml の「{label}」の smoking は \"禁煙\" \"喫煙\" \"どちらでも\" のどれかにしてください"
            )

        room_types = [str(r).strip() for r in _as_list(w.get("room_types"), label, "room_types") if str(r).strip()]
        unknown = [r for r in room_types if r not in ROOM_TYPE_KEYWORDS]
        if unknown:
            raise ConfigError(
                f"watches.toml の「{label}」の room_types に使えない値があります: {', '.join(unknown)}"
                f"（使える値: {', '.join(ROOM_TYPE_KEYWORDS)}）"
            )

        exclude_hotels = _as_list(w.get("exclude_hotels"), label, "exclude_hotels")
        exclude_nos = {int(x) for x in exclude_hotels if isinstance(x, int) or str(x).isdigit()}
        exclude_names = [str(x).strip() for x in exclude_hotels if not (isinstance(x, int) or str(x).isdigit()) and str(x).strip()]
        extra_keywords = [str(x).strip() for x in _as_list(w.get("exclude_keywords"), label, "exclude_keywords") if str(x).strip()]

        normalized.append(
            {
                "name": label,
                "checkin": checkin,
                "checkout": checkout,
                "adults": int(w.get("adults", 1)),
                "rooms": int(w.get("rooms", 1)),
                "stations": stations,
                "radius_km": radius,
                "hotels": hotels,
                "min_price": min_price,
                "max_price": max_price,
                "smoking": smoking,
                "room_types": room_types,
                "exclude_capsule": bool(w.get("exclude_capsule", True)),
                "exclude_nos": exclude_nos,
                "exclude_names": exclude_names,
                "exclude_keywords": extra_keywords,
                "enabled": bool(w.get("enabled", True)),
            }
        )
    return normalized


def watch_key(w: dict) -> str:
    """条件が変わったら別の監視として扱う（前回の結果を引き継がない）。"""
    parts = [
        w["checkin"], w["checkout"], w["adults"], w["rooms"],
        ",".join(w["stations"]), w["radius_km"], ",".join(map(str, w["hotels"])),
        w["min_price"], w["max_price"], w["smoking"], ",".join(w["room_types"]),
        w["exclude_capsule"],
    ]
    return "|".join(str(p) for p in parts)


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ================================================================ HTTP


def rakuten_headers(referer: str) -> dict:
    """楽天の新API（2026年〜）は Referer と Origin の両方がないと 403 を返す。
    Origin は「https://github.com」のようにスキーム＋ドメインだけにする。"""
    if not referer:
        return {}
    parsed = urllib.parse.urlparse(referer if "://" in referer else "https://" + referer)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    ref = referer if "://" in referer else "https://" + referer
    if not urllib.parse.urlparse(ref).path:
        ref += "/"
    return {"Referer": ref, "Origin": origin}


def api_error_text(data: dict) -> tuple[str, str]:
    """楽天のエラーは {"error", "error_description"} 形式と
    {"errors": {"errorCode", "errorMessage"}} 形式の2通りがあるので両方読む。"""
    errs = data.get("errors")
    if isinstance(errs, list) and errs:
        errs = errs[0]
    if isinstance(errs, dict):
        return str(errs.get("errorCode", "")), str(errs.get("errorMessage", ""))
    return str(data.get("error", "")), str(data.get("error_description", ""))


def http_get_json(url: str, headers: dict | None = None, retries: int = 3) -> tuple[int, dict]:
    """(HTTPステータス, JSON) を返す。ネットワーク障害・一時エラーはリトライする。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {"error": "unknown", "error_description": body[:200]}
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            return e.code, data
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"接続できませんでした（{url.split('?')[0]}）: {getattr(e, 'reason', e)}") from e
    raise RuntimeError("接続に失敗しました")


# ================================================================ 駅の位置


def parse_station(spec: str) -> tuple[str, str]:
    """「鶴見/神奈川県」→ ("鶴見", "神奈川県")。駅名の「駅」は外す。"""
    name, _, pref = spec.partition("/")
    name = name.strip()
    if name.endswith("駅") and len(name) > 1:
        name = name[:-1]
    return name, pref.strip()


def lookup_station(spec: str, cache: dict) -> dict:
    """駅名から緯度経度を調べる（HeartRails Express API）。結果は state にキャッシュする。"""
    if spec in cache:
        return cache[spec]
    name, pref = parse_station(spec)
    params = {"method": "getStations", "name": name}
    if pref:
        params["prefecture"] = pref
    status, data = http_get_json(STATION_API_URL + "?" + urllib.parse.urlencode(params))
    stations = (data.get("response") or {}).get("station") or []
    if status != 200 or not stations:
        raise ConfigError(
            f"駅「{spec}」が見つかりませんでした。駅名を確認してください"
            "（「駅」は付けても付けなくてもOK。例: \"蒲田\"、\"鶴見/神奈川県\"）"
        )
    prefs = sorted({s.get("prefecture", "") for s in stations})
    if len(prefs) > 1:
        options = "、".join(f"\"{name}/{p}\"" for p in prefs)
        raise ConfigError(f"駅「{spec}」は複数の都道府県にあります。{options} のように都道府県を付けてください")
    # 同じ駅名で路線ごとに複数返るので平均を取る
    lat = sum(float(s["y"]) for s in stations) / len(stations)
    lon = sum(float(s["x"]) for s in stations) / len(stations)
    result = {"name": name, "prefecture": prefs[0], "lat": round(lat, 6), "lon": round(lon, 6)}
    cache[spec] = result
    return result


def distance_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ================================================================ 楽天API


def _collect(obj, key: str, out: list) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            else:
                _collect(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, key, out)


def _parse_hotels(data: dict) -> list[dict]:
    """レスポンスを [{"basic": {...}, "rooms": [{"room": {...}, "charge": {...}}, ...]}] に整える。"""
    hotels = []
    for entry in data.get("hotels", []) or []:
        parts = entry.get("hotel", entry) if isinstance(entry, dict) else entry
        if isinstance(parts, dict):
            parts = [parts]
        basic, rooms = {}, []
        for part in parts or []:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("hotelBasicInfo"), dict):
                basic = part["hotelBasicInfo"]
            if "roomInfo" in part:
                room, charge = {}, {}
                infos = part["roomInfo"] if isinstance(part["roomInfo"], list) else [part["roomInfo"]]
                for info in infos:
                    if isinstance(info, dict):
                        room = info.get("roomBasicInfo", room) or room
                        charge = info.get("dailyCharge", charge) or charge
                if room:
                    rooms.append({"room": room, "charge": charge})
        if basic:
            hotels.append({"basic": basic, "rooms": rooms})
    return hotels


def rakuten_search(w: dict, location: dict, creds: dict) -> list[dict]:
    """1エリア（または施設番号リスト）について、空室のあるホテル・プランを取得する。"""
    base = {
        "applicationId": creds["app_id"],
        "accessKey": creds["access_key"],
        "format": "json",
        "checkinDate": w["checkin"].isoformat(),
        "checkoutDate": w["checkout"].isoformat(),
        "adultNum": w["adults"],
        "roomNum": w["rooms"],
        "searchPattern": 1,      # 宿泊プランごとに取得（部屋タイプ・喫煙で絞るため）
        "hits": 30,
        "sort": "+roomCharge",   # 安い順
        "datumType": 1,          # 緯度経度は「度」で扱う
    }
    if w["smoking"] == "禁煙":
        base["squeezeCondition"] = "kinen"
    base.update(location)

    headers = rakuten_headers(creds["referer"])
    results: list[dict] = []
    for page in range(1, MAX_PAGES_PER_SEARCH + 1):
        params = {**base, "page": page}
        status, data = http_get_json(RAKUTEN_VACANT_URL + "?" + urllib.parse.urlencode(params), headers)
        time.sleep(API_INTERVAL_SEC)
        code, message = api_error_text(data)
        if status == 404 or code == "not_found" or "not_found" in message.lower():
            break
        if status in (400, 401, 403):
            hint = "アプリID・アクセスキー・設定値を確認してください"
            if "REFERR" in message.upper() or "ORIGIN" in message.upper() or status == 403:
                hint = ("楽天アプリの「許可されたWebサイト」に github.com が登録されているか確認してください"
                        f"（送信した Origin: {headers.get('Origin', 'なし')}）")
            raise ConfigError(f"楽天APIがリクエストを拒否しました（{status} {code}: {message}）。{hint}")
        if status != 200:
            raise RuntimeError(f"楽天APIエラー {status} {code}: {message}")

        hotels = _parse_hotels(data)
        results.extend(hotels)

        paging = data.get("pagingInfo", {}) or {}
        if page >= int(paging.get("pageCount", 1) or 1):
            break
        # 安い順に並んでいるので、このページが全部上限超えなら次のページも上限超え
        if w["max_price"]:
            prices = [per_room_price(r["charge"], w) for h in hotels for r in h["rooms"]]
            prices = [p for p in prices if p is not None]
            if prices and min(prices) > w["max_price"]:
                break
    return results


# ================================================================ 条件の判定


def per_room_price(charge: dict, w: dict) -> int | None:
    """1部屋・1泊あたりの料金（税込）。"""
    total = charge.get("total")
    try:
        total = int(total)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return round(total / max(w["rooms"], 1))


def _contains(text: str, words) -> bool:
    low = text.lower()
    return any(word.lower() in low for word in words)


def room_type_ok(text: str, room_types: list[str]) -> bool:
    if not room_types:
        return True
    low = text.lower()
    for rt in room_types:
        if rt == "ダブル":
            # 「セミダブル」を「ダブル」と誤判定しない
            stripped = low
            for kw in ROOM_TYPE_KEYWORDS["セミダブル"]:
                stripped = stripped.replace(kw.lower(), "")
            if _contains(stripped, ROOM_TYPE_KEYWORDS["ダブル"]):
                return True
        elif _contains(low, ROOM_TYPE_KEYWORDS[rt]):
            return True
    return False


def smoking_ok(text: str, smoking: str) -> bool:
    if smoking == "禁煙":
        # 楽天側でも禁煙で絞っているが、念のため「喫煙」とだけ書かれた部屋を外す
        return not ("喫煙" in text and "禁煙" not in text)
    if smoking == "喫煙":
        return "喫煙" in text and "禁煙" not in text
    return True


def hotel_excluded(basic: dict, w: dict) -> str | None:
    """除外する理由を返す（除外しないなら None）。"""
    try:
        no = int(basic.get("hotelNo", 0))
    except (TypeError, ValueError):
        no = 0
    name = str(basic.get("hotelName", ""))
    if no in w["exclude_nos"]:
        return "除外リスト（番号）"
    for ex in w["exclude_names"]:
        if ex and ex in name:
            return f"除外リスト（{ex}）"
    hotel_text = f"{name} {basic.get('hotelSpecial', '')}"
    if w["exclude_capsule"] and _contains(hotel_text, CAPSULE_KEYWORDS):
        return "カプセル・相部屋タイプ"
    if w["exclude_keywords"] and _contains(hotel_text, w["exclude_keywords"]):
        return "除外キーワード"
    return None


def filter_hotels(raw: list[dict], w: dict, origins: dict) -> tuple[dict, dict]:
    """条件に合うホテルを {hotelNo: 情報} で返す。除外したホテルも理由付きで返す。"""
    matched: dict[str, dict] = {}
    excluded: dict[str, str] = {}
    for h in raw:
        basic = h["basic"]
        no = str(basic.get("hotelNo", ""))
        if not no:
            continue
        reason = hotel_excluded(basic, w)
        if reason:
            excluded[no] = f"{basic.get('hotelName', '')}（{reason}）"
            continue

        for r in h["rooms"]:
            room, charge = r["room"], r["charge"]
            text = f"{room.get('roomName', '')} {room.get('planName', '')}"
            if w["exclude_capsule"] and _contains(text, CAPSULE_KEYWORDS):
                continue
            if not smoking_ok(text, w["smoking"]) or not room_type_ok(text, w["room_types"]):
                continue
            price = per_room_price(charge, w)
            if price is None:
                continue
            if w["min_price"] and price < w["min_price"]:
                continue
            if w["max_price"] and price > w["max_price"]:
                continue

            cur = matched.get(no)
            if cur is None:
                cur = matched[no] = {
                    "name": basic.get("hotelName", ""),
                    "price": price,
                    "room_name": room.get("roomName", ""),
                    "url": basic.get("planListUrl") or room.get("reserveUrl", ""),
                    "station": basic.get("nearestStation", ""),
                    "plans": 0,
                    "near": "",
                }
                lat, lon = basic.get("latitude"), basic.get("longitude")
                try:
                    best = None
                    for label, o in origins.items():
                        d = distance_km(float(lat), float(lon), o["lat"], o["lon"])
                        if best is None or d < best[1]:
                            best = (label, d)
                    if best:
                        cur["near"] = f"{best[0]}駅から約{best[1]:.1f}km"
                except (TypeError, ValueError):
                    pass
            cur["plans"] += 1
            if price < cur["price"]:
                cur.update(price=price, room_name=room.get("roomName", ""))
    return matched, excluded


# ================================================================ LINE


def send_line(text: str, token: str, user_id: str) -> None:
    payload = {"to": user_id, "messages": [{"type": "text", "text": text[:4900]}]}
    req = urllib.request.Request(
        LINE_PUSH_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        if e.code in (400, 401, 403):
            raise ConfigError(
                f"LINEへの送信に失敗しました（{e.code}）: {body}\n"
                "チャネルアクセストークンとユーザーIDを確認し、公式アカウントを友だち追加しているか確認してください"
            ) from e
        if e.code == 429:
            raise RuntimeError(f"LINEの送信上限に達した可能性があります（429）: {body}") from e
        raise RuntimeError(f"LINEへの送信に失敗しました（{e.code}）: {body}") from e


def weekday_ja(d: dt.date) -> str:
    return "月火水木金土日"[d.weekday()]


def describe_conditions(w: dict) -> str:
    bits = []
    if w["min_price"] or w["max_price"]:
        lo = f"{w['min_price']:,}円" if w["min_price"] else ""
        hi = f"{w['max_price']:,}円" if w["max_price"] else ""
        bits.append(f"{lo}〜{hi}")
    if w["smoking"] != "どちらでも":
        bits.append(w["smoking"])
    if w["room_types"]:
        bits.append("・".join(w["room_types"]))
    return " / ".join(bits)


def format_message(w: dict, new_hotels: list[dict]) -> str:
    nights = (w["checkout"] - w["checkin"]).days
    lines = [
        f"🏨 空室が出ました（{len(new_hotels)}件）",
        f"【{w['name']}】",
        f"{w['checkin']:%m/%d}({weekday_ja(w['checkin'])}) から {nights}泊・大人{w['adults']}名・{w['rooms']}室",
    ]
    cond = describe_conditions(w)
    if cond:
        lines.append(f"条件: {cond}")
    for h in new_hotels[:MAX_HOTELS_IN_MESSAGE]:
        lines.append("")
        lines.append(f"■ {h['name']}")
        detail = f"{h['room_name']} {h['price']:,}円〜/室・1泊"
        lines.append(detail)
        if h["near"]:
            lines.append(h["near"])
        if h["url"]:
            lines.append(h["url"])
    if len(new_hotels) > MAX_HOTELS_IN_MESSAGE:
        lines.append("")
        lines.append(f"ほか {len(new_hotels) - MAX_HOTELS_IN_MESSAGE} 件（安い順に{MAX_HOTELS_IN_MESSAGE}件を表示）")
    lines.append("")
    lines.append("※早い者勝ちです。予約は自分で完了させてください。")
    return "\n".join(lines)


# ================================================================ メイン


def check_watch(w: dict, creds: dict, station_cache: dict) -> tuple[dict, dict]:
    raw: list[dict] = []
    origins: dict[str, dict] = {}
    if w["hotels"]:
        raw += rakuten_search(w, {"hotelNo": ",".join(map(str, w["hotels"]))}, creds)
    for spec in w["stations"]:
        st = lookup_station(spec, station_cache)
        origins[st["name"]] = st
        raw += rakuten_search(
            w, {"latitude": st["lat"], "longitude": st["lon"], "searchRadius": w["radius_km"]}, creds
        )
    return filter_hotels(raw, w, origins)


def run(dry_run: bool) -> int:
    creds = {
        "app_id": env("RAKUTEN_APP_ID"),
        "access_key": env("RAKUTEN_ACCESS_KEY"),
        "referer": env("RAKUTEN_REFERER", required=False),
    }
    if not dry_run:
        line_token = env("LINE_CHANNEL_ACCESS_TOKEN")
        line_user = env("LINE_USER_ID")

    watches = load_watches()
    state = load_state()
    station_cache = state.setdefault("_stations", {})
    watch_state = state.setdefault("watches", {})
    today = dt.datetime.now(JST).date()
    now_str = dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    had_error = False
    active_keys = set()

    for w in watches:
        key = watch_key(w)
        active_keys.add(key)
        label = f"{w['name']} {w['checkin']}〜{w['checkout']}"
        if not w["enabled"]:
            print(f"⏸  {label}: enabled = false のためスキップ")
            continue
        if w["checkin"] < today:
            print(f"⏭  {label}: チェックイン日を過ぎたためスキップ（watches.toml から削除してOK）")
            continue

        try:
            matched, excluded = check_watch(w, creds, station_cache)
        except RuntimeError as e:
            had_error = True
            print(f"⚠️  {label}: {e}")
            continue

        prev = set(watch_state.get(key, {}).get("hotels", []))
        new_nos = [no for no in matched if no not in prev]
        new_hotels = sorted((matched[no] for no in new_nos), key=lambda h: h["price"])

        print(f"{'✅' if matched else '…'} {label}: 条件に合う空室 {len(matched)}件（新しく空いた {len(new_hotels)}件）")
        if dry_run:
            for no, h in sorted(matched.items(), key=lambda kv: kv[1]["price"]):
                near = f" / {h['near']}" if h["near"] else ""
                print(f"     {no:>8}  {h['name']}  {h['room_name']} {h['price']:,}円{near}")
            if excluded:
                print("   除外したホテル:")
                for no, why in excluded.items():
                    print(f"     {no:>8}  {why}")

        if new_hotels:
            message = format_message(w, new_hotels)
            if dry_run:
                print("---- 通知内容（dry-run のため送信しません）----")
                print(message)
                print("-----------------------------------------------")
            else:
                send_line(message, line_token, line_user)
                print("   → LINEに通知しました")

        watch_state[key] = {"name": w["name"], "hotels": sorted(matched), "checked_at": now_str}

    for k in list(watch_state):
        if k not in active_keys:
            del watch_state[k]
    if not dry_run:
        save_state(state)
    return 1 if had_error else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test-line", action="store_true", help="LINEにテスト通知を送る")
    parser.add_argument("--dry-run", action="store_true", help="LINEに送らず、条件に合うホテルを一覧表示する")
    args = parser.parse_args()

    try:
        if args.test_line:
            send_line(
                "✅ ホテル空室通知のテストです。\nこのメッセージが届いていれば、LINE通知の設定は完了しています。",
                env("LINE_CHANNEL_ACCESS_TOKEN"),
                env("LINE_USER_ID"),
            )
            print("LINEにテスト通知を送りました")
            return 0
        return run(dry_run=args.dry_run)
    except ConfigError as e:
        print(f"❌ 設定エラー: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
