#!/usr/bin/env python3
"""ホテル名から楽天トラベルの施設番号（hotel_no）を調べる。

GitHub Actions の「ホテル番号を調べる」ワークフローから実行するか、手元で:
    RAKUTEN_APP_ID=... RAKUTEN_ACCESS_KEY=... python find_hotel.py "東横INN 甲府"
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

KEYWORD_URL = "https://openapi.rakuten.co.jp/engine/api/Travel/KeywordHotelSearch/20260731"


def collect(obj, key: str, out: list) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            else:
                collect(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            collect(v, key, out)


def main() -> int:
    keyword = " ".join(sys.argv[1:]).strip() or os.environ.get("KEYWORD", "").strip()
    if len(keyword) < 2:
        print("キーワードを2文字以上で指定してください（例: 東横INN 甲府）", file=sys.stderr)
        return 2
    app_id = os.environ.get("RAKUTEN_APP_ID", "").strip()
    access_key = os.environ.get("RAKUTEN_ACCESS_KEY", "").strip()
    if not app_id or not access_key:
        print("RAKUTEN_APP_ID と RAKUTEN_ACCESS_KEY を設定してください", file=sys.stderr)
        return 2

    params = {
        "applicationId": app_id,
        "accessKey": access_key,
        "format": "json",
        "keyword": keyword,
        "searchField": 1,  # ホテル名のみで検索
        "hits": 30,
    }
    headers = {"User-Agent": "hotel-vacancy-notifier/2.0 (personal use)"}
    referer = os.environ.get("RAKUTEN_REFERER", "").strip()
    if referer:
        # 楽天の新APIは Referer と Origin の両方がないと 403 になる
        parsed = urllib.parse.urlparse(referer if "://" in referer else "https://" + referer)
        headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        headers["Referer"] = referer if "://" in referer else "https://" + referer
    req = urllib.request.Request(KEYWORD_URL + "?" + urllib.parse.urlencode(params), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        if e.code == 404:
            print(f"「{keyword}」に一致するホテルは見つかりませんでした。キーワードを短くして試してください。")
            return 0
        print(f"楽天APIエラー（{e.code}）: {body}", file=sys.stderr)
        if e.code == 403:
            print("楽天アプリの「許可されたWebサイト」に github.com が登録されているか確認してください", file=sys.stderr)
        return 1

    basics: list = []
    collect(data, "hotelBasicInfo", basics)
    if not basics:
        print(f"「{keyword}」に一致するホテルは見つかりませんでした。")
        return 0

    print(f"「{keyword}」の検索結果（{len(basics)}件）")
    print("hotel_no   ホテル名 / 住所")
    print("-" * 60)
    for b in basics:
        if isinstance(b, dict):
            addr = f"{b.get('address1', '')}{b.get('address2', '')}"
            print(f"{b.get('hotelNo', ''):<10} {b.get('hotelName', '')}\n           {addr}")
    print("-" * 60)
    print("左端の番号を watches.toml の hotel_no に書いてください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
