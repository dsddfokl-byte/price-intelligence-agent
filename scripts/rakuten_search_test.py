#!/usr/bin/env python3
"""楽天市場商品検索APIへの接続を確認するスクリプト。"""

import os
import sys
from pathlib import Path
from typing import Any, Optional, Tuple

import requests
from dotenv import load_dotenv


API_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
REQUIRED_ENV_VARS = (
    "RAKUTEN_APP_ID",
    "RAKUTEN_ACCESS_KEY",
    "RAKUTEN_AFFILIATE_ID",
)


def redact_secrets(value: Any) -> str:
    """表示値に認証情報が含まれていた場合は伏字にする。"""
    text = str(value)
    for name in REQUIRED_ENV_VARS:
        secret = os.getenv(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def api_error_fields(response: requests.Response) -> Tuple[Optional[str], Optional[str]]:
    """楽天APIのerrorとerror_descriptionだけを安全に取り出す。"""
    try:
        body = response.json()
    except (requests.exceptions.JSONDecodeError, ValueError):
        return None, None

    if isinstance(body, dict):
        error = body.get("error")
        description = body.get("error_description")
        safe_error = redact_secrets(error) if error is not None else None
        safe_description = (
            redact_secrets(description).replace("\n", " ").replace("\r", " ")
            if description is not None
            else None
        )
        return safe_error, safe_description
    return None, None


def main() -> int:
    # 呼び出し元の環境ではなく、プロジェクト直下の.envを確実に使用する。
    load_dotenv(dotenv_path=ENV_FILE, override=True)

    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        print(
            "必要な環境変数が設定されていません: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 1

    params = {
        "applicationId": os.environ["RAKUTEN_APP_ID"],
        "affiliateId": os.environ["RAKUTEN_AFFILIATE_ID"],
        "keyword": "猫 フード",
        "hits": 5,
    }
    headers = {"accessKey": os.environ["RAKUTEN_ACCESS_KEY"]}

    try:
        response = requests.get(
            API_URL,
            params=params,
            headers=headers,
            timeout=30,
        )
    except requests.Timeout:
        print("HTTP status: 取得不能")
        print("接続エラー: リクエストがタイムアウトしました。", file=sys.stderr)
        return 1
    except requests.RequestException:
        # 例外文字列にはリクエストURLが含まれ得るため表示しない。
        print("HTTP status: 取得不能")
        print("接続エラー: APIへのリクエストに失敗しました。", file=sys.stderr)
        return 1

    print(f"HTTP status: {response.status_code}")

    if response.status_code != 200:
        error, description = api_error_fields(response)
        if error is not None:
            print(f"error: {error}")
        if description is not None:
            print(f"error_description: {description}")
        return 1

    print("API接続成功")

    try:
        payload = response.json()
    except (requests.exceptions.JSONDecodeError, ValueError):
        print("応答エラー: JSONを解析できませんでした。", file=sys.stderr)
        return 1

    if not isinstance(payload, dict):
        print("応答エラー: JSONの形式が想定と異なります。", file=sys.stderr)
        return 1

    count = payload.get("count", "N/A")
    hits = payload.get("hits", "N/A")
    items = payload.get("Items", payload.get("items", []))

    print(f"count: {redact_secrets(count)}")
    print(f"hits: {redact_secrets(hits)}")

    if not items:
        print("検索結果なし")
        return 0

    first_entry = items[0]
    if not isinstance(first_entry, dict):
        print("応答エラー: 商品情報の形式が想定と異なります。", file=sys.stderr)
        return 1

    # formatVersion=1（デフォルト）と2の両方を安全に扱う。
    first_item = first_entry.get("Item", first_entry.get("item", first_entry))
    if not isinstance(first_item, dict):
        print("応答エラー: 商品情報の形式が想定と異なります。", file=sys.stderr)
        return 1

    print(f"最初の商品名: {redact_secrets(first_item.get('itemName', 'N/A'))}")
    print(f"最初の商品の価格: {redact_secrets(first_item.get('itemPrice', 'N/A'))}円")

    return 0


if __name__ == "__main__":
    sys.exit(main())
