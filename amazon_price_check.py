import argparse
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "alerts.db"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

PRICE_SELECTORS = [
    "#corePriceDisplay_desktop_feature_div span.a-price",
    "#corePrice_feature_div span.a-price",
    "#apex_desktop span.a-price",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    "#priceblock_saleprice",
]

TITLE_SELECTORS = [
    "#productTitle",
    "#title",
]

HELP_TEXT = (
    "Commands:\n"
    "/add <amazon_url> <target_price>\n"
    "/list\n"
    "/remove <id>\n"
    "/help"
)

PRICE_SCALE = Decimal("0.01")


@dataclass
class Settings:
    telegram_token: str
    telegram_chat_id: str
    db_path: Path
    webhook_secret: str


@dataclass
class ProductPage:
    title: str
    price_text: str
    price_minor: int
    canonical_url: str


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_settings() -> Settings:
    load_dotenv()

    telegram_token = os.getenv("TELEGRAM_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    db_path_value = os.getenv("ALERTS_DB_PATH")
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    db_path = Path(db_path_value) if db_path_value else DB_PATH

    if not telegram_token:
        raise ValueError("Set TELEGRAM_TOKEN in your environment or .env file.")

    if not telegram_chat_id:
        raise ValueError("Set TELEGRAM_CHAT_ID in your environment or .env file.")

    if not webhook_secret:
        raise ValueError("Set TELEGRAM_WEBHOOK_SECRET in your environment or .env file.")

    return Settings(
        telegram_token=telegram_token.strip(),
        telegram_chat_id=telegram_chat_id.strip(),
        db_path=db_path,
        webhook_secret=webhook_secret.strip(),
    )


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            url TEXT NOT NULL,
            canonical_url TEXT NOT NULL UNIQUE,
            product_title TEXT,
            target_price_minor INTEGER NOT NULL,
            last_seen_price_minor INTEGER,
            last_checked_at TEXT,
            last_alert_sent_at TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.commit()


def fetch_listing_response(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response


def parse_price_from_price_node(price_node: Any) -> Optional[str]:
    whole = price_node.select_one(".a-price-whole")
    fraction = price_node.select_one(".a-price-fraction")
    symbol = price_node.select_one(".a-price-symbol")

    if whole:
        price = whole.get_text(strip=True).replace(",", "")
        if fraction:
            price = f"{price}.{fraction.get_text(strip=True)}"
        if symbol:
            return f"{symbol.get_text(strip=True)}{price}"
        return price

    text = " ".join(price_node.stripped_strings)
    return normalize_price_text(text)


def normalize_price_text(text: str) -> Optional[str]:
    compact = " ".join(text.split())
    match = re.search(r"([₹$€£]\s?[\d,]+(?:\.\d{2})?)", compact)
    if match:
        return match.group(1).replace(" ", "")

    match = re.search(r"([\d,]+(?:\.\d{2})?)", compact)
    if match:
        return match.group(1)

    return None


def parse_price_minor_from_text(price_text: str) -> int:
    cleaned = re.sub(r"[^\d.]", "", price_text)
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Could not parse numeric price from: {price_text}") from exc

    return decimal_to_minor_units(amount)


def decimal_to_minor_units(amount: Decimal) -> int:
    quantized = amount.quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)
    return int((quantized * 100).to_integral_value(rounding=ROUND_HALF_UP))


def format_minor_units(amount_minor: int) -> str:
    amount = Decimal(amount_minor) / Decimal("100")
    return format_decimal_price(amount)


def format_decimal_price(price: Decimal) -> str:
    normalized = price.quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")


def extract_price(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")

    for selector in PRICE_SELECTORS:
        node = soup.select_one(selector)
        if not node:
            continue

        price = parse_price_from_price_node(node)
        if price:
            return price

        price = normalize_price_text(node.get_text(" ", strip=True))
        if price:
            return price

    return None


def extract_title(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")

    for selector in TITLE_SELECTORS:
        node = soup.select_one(selector)
        if not node:
            continue

        title = node.get_text(" ", strip=True)
        if title:
            return title

    return None


def build_canonical_url(final_url: str) -> str:
    parsed = urlparse(final_url)
    asin_match = re.search(r"/dp/([A-Z0-9]{10})", parsed.path, re.IGNORECASE)
    if asin_match:
        asin = asin_match.group(1).upper()
        return f"{parsed.scheme}://{parsed.netloc}/dp/{asin}"

    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def fetch_product_page(url: str) -> ProductPage:
    response = fetch_listing_response(url)
    html = response.text
    title = extract_title(html) or "Unknown product"
    price_text = extract_price(html)

    if not price_text:
        raise RuntimeError(
            "Could not find a price on the page. Amazon may have changed the markup "
            "or returned a bot/CAPTCHA page."
        )

    return ProductPage(
        title=title,
        price_text=price_text,
        price_minor=parse_price_minor_from_text(price_text),
        canonical_url=build_canonical_url(response.url),
    )


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text},
        timeout=20,
    )
    response.raise_for_status()


def parse_target_price(raw_value: str) -> int:
    cleaned = raw_value.replace(",", "").strip()
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Target price must be a valid number.") from exc

    if amount <= 0:
        raise ValueError("Target price must be greater than zero.")

    return decimal_to_minor_units(amount)


def upsert_alert(
    connection: sqlite3.Connection,
    chat_id: str,
    original_url: str,
    product: ProductPage,
    target_price_minor: int,
) -> tuple[int, bool]:
    now = now_utc_iso()
    existing = connection.execute(
        "SELECT id FROM alerts WHERE canonical_url = ?",
        (product.canonical_url,),
    ).fetchone()

    if existing:
        connection.execute(
            """
            UPDATE alerts
            SET chat_id = ?,
                url = ?,
                product_title = ?,
                target_price_minor = ?,
                last_seen_price_minor = ?,
                last_checked_at = ?,
                is_active = 1,
                updated_at = ?
            WHERE id = ?
            """,
            (
                chat_id,
                original_url,
                product.title,
                target_price_minor,
                product.price_minor,
                now,
                now,
                existing["id"],
            ),
        )
        connection.commit()
        return int(existing["id"]), False

    cursor = connection.execute(
        """
        INSERT INTO alerts (
            chat_id,
            url,
            canonical_url,
            product_title,
            target_price_minor,
            last_seen_price_minor,
            last_checked_at,
            is_active,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            chat_id,
            original_url,
            product.canonical_url,
            product.title,
            target_price_minor,
            product.price_minor,
            now,
            now,
            now,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid), True


def list_active_alerts(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = connection.execute(
        """
        SELECT
            id,
            product_title,
            canonical_url,
            target_price_minor,
            last_seen_price_minor,
            last_checked_at
        FROM alerts
        WHERE is_active = 1
        ORDER BY id
        """
    ).fetchall()
    return list(rows)


def deactivate_alert(connection: sqlite3.Connection, alert_id: int) -> bool:
    now = now_utc_iso()
    cursor = connection.execute(
        """
        UPDATE alerts
        SET is_active = 0,
            updated_at = ?
        WHERE id = ? AND is_active = 1
        """,
        (now, alert_id),
    )
    connection.commit()
    return cursor.rowcount > 0


def get_active_alert_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = connection.execute(
        """
        SELECT
            id,
            chat_id,
            url,
            canonical_url,
            product_title,
            target_price_minor
        FROM alerts
        WHERE is_active = 1
        ORDER BY id
        """
    ).fetchall()
    return list(rows)


def update_alert_check_result(
    connection: sqlite3.Connection,
    alert_id: int,
    product: ProductPage,
) -> None:
    now = now_utc_iso()
    connection.execute(
        """
        UPDATE alerts
        SET url = ?,
            canonical_url = ?,
            product_title = ?,
            last_seen_price_minor = ?,
            last_checked_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            product.canonical_url,
            product.canonical_url,
            product.title,
            product.price_minor,
            now,
            now,
            alert_id,
        ),
    )
    connection.commit()


def mark_alert_sent(connection: sqlite3.Connection, alert_id: int) -> None:
    now = now_utc_iso()
    connection.execute(
        """
        UPDATE alerts
        SET last_alert_sent_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (now, now, alert_id),
    )
    connection.commit()


def build_list_message(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "No active alerts."

    lines = ["Active alerts:"]
    for row in rows:
        current = (
            format_minor_units(row["last_seen_price_minor"])
            if row["last_seen_price_minor"] is not None
            else "unknown"
        )
        lines.append(
            (
                f"{row['id']}. {row['product_title'] or 'Unknown product'}\n"
                f"Target: {format_minor_units(row['target_price_minor'])} | "
                f"Last seen: {current}\n"
                f"{row['canonical_url']}"
            )
        )
    return "\n\n".join(lines)


def parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


def process_add_command(
    connection: sqlite3.Connection,
    settings: Settings,
    args: list[str],
) -> str:
    if len(args) < 2:
        return "Usage: /add <amazon_url> <target_price>"

    url = args[0]
    try:
        target_price_minor = parse_target_price(args[1])
        product = fetch_product_page(url)
        alert_id, created = upsert_alert(
            connection,
            settings.telegram_chat_id,
            url,
            product,
            target_price_minor,
        )
    except Exception as exc:
        return f"Failed to add alert: {exc}"

    action = "Created" if created else "Updated"
    return (
        f"{action} alert #{alert_id}\n"
        f"{product.title}\n"
        f"Current price: {product.price_text}\n"
        f"Target price: {format_minor_units(target_price_minor)}\n"
        f"{product.canonical_url}"
    )


def process_remove_command(connection: sqlite3.Connection, args: list[str]) -> str:
    if len(args) != 1:
        return "Usage: /remove <id>"

    try:
        alert_id = int(args[0])
    except ValueError:
        return "Alert ID must be an integer."

    removed = deactivate_alert(connection, alert_id)
    if not removed:
        return f"No active alert found with ID {alert_id}."
    return f"Removed alert #{alert_id}."


def process_message(
    connection: sqlite3.Connection,
    settings: Settings,
    message_text: str,
) -> str:
    command, args = parse_command(message_text)

    if command in {"/start", "/help"}:
        return HELP_TEXT
    if command == "/add":
        return process_add_command(connection, settings, args)
    if command == "/list":
        return build_list_message(list_active_alerts(connection))
    if command == "/remove":
        return process_remove_command(connection, args)

    return f"Unknown command.\n\n{HELP_TEXT}"


def handle_update(update: dict[str, Any], settings: Settings) -> None:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    text = message.get("text", "")
    chat_id = str(chat.get("id", ""))

    if chat_id != settings.telegram_chat_id or not text:
        return

    connection = get_connection(settings.db_path)
    try:
        init_db(connection)
        reply = process_message(connection, settings, text)
    finally:
        connection.close()

    send_telegram_message(settings.telegram_token, settings.telegram_chat_id, reply)


def run_check(settings: Settings) -> None:
    connection = get_connection(settings.db_path)
    init_db(connection)

    alerts = get_active_alert_rows(connection)
    if not alerts:
        print("No active alerts to check.")
        connection.close()
        return

    for alert in alerts:
        try:
            product = fetch_product_page(alert["url"])
            update_alert_check_result(connection, int(alert["id"]), product)
            print(
                f"#{alert['id']} {product.title}: "
                f"{product.price_text} (target: {format_minor_units(alert['target_price_minor'])})"
            )

            if product.price_minor <= alert["target_price_minor"]:
                message = (
                    f"Price alert\n"
                    f"{product.title}\n"
                    f"Current price: {product.price_text}\n"
                    f"Target price: {format_minor_units(alert['target_price_minor'])}\n"
                    f"{product.canonical_url}"
                )
                send_telegram_message(
                    settings.telegram_token,
                    settings.telegram_chat_id,
                    message,
                )
                mark_alert_sent(connection, int(alert["id"]))
                print(f"Alert sent for #{alert['id']}.")
            else:
                print(f"Target not reached for #{alert['id']}.")
        except Exception as exc:
            print(f"Failed for alert #{alert['id']} ({alert['url']}): {exc}")

    connection.close()


def create_app() -> Flask:
    settings = load_settings()
    app = Flask(__name__)

    connection = get_connection(settings.db_path)
    try:
        init_db(connection)
    finally:
        connection.close()

    @app.post(f"/telegram/webhook/{settings.webhook_secret}")
    def telegram_webhook() -> tuple[Any, int]:
        if not request.is_json:
            abort(400, description="Expected JSON payload.")

        update = request.get_json(silent=True)
        if not isinstance(update, dict):
            abort(400, description="Invalid Telegram update payload.")

        handle_update(update, settings)
        return jsonify({"ok": True}), 200

    @app.get("/health")
    def health() -> tuple[Any, int]:
        return jsonify({"ok": True}), 200

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Amazon price checker with a Telegram webhook and SQLite storage."
    )
    parser.add_argument(
        "mode",
        choices=["check", "serve"],
        help="Use 'serve' to run the Flask webhook server or 'check' to evaluate saved alerts.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for Flask serve mode.")
    parser.add_argument("--port", type=int, default=8000, help="Port for Flask serve mode.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = load_settings()

    if args.mode == "check":
        run_check(settings)
        return

    if args.mode == "serve":
        app = create_app()
        app.run(host=args.host, port=args.port)
        return

    raise ValueError(f"Unsupported mode: {args.mode}")


if __name__ == "__main__":
    main()
