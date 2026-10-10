"""Импорт PDF-выписок Сбера и Альфа-Банка в таблицу transactions.

    python import_statements.py <папка_с_pdf> [--db finance.db] [--force] [--no-backup]

Банк и счёт определяются по содержимому PDF, а не по имени файла. Каждая выписка сверяется со своей
шапкой (остатки, поступления, расходы): если итоги не сходятся, файл пропускается (--force загрузит всё равно).
Повторная загрузка безопасна: операции с тем же fingerprint обновляются, а не дублируются.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pdfplumber

from . import alfa, sber
from . import normalize as nz
from .schema import TRANSACTIONS_SCHEMA

COLUMNS = ("source", "fingerprint", "date", "posted", "amount_kop", "type", "category", "merchant",
           "description", "mcc", "bank_category", "counterparty", "card_last4", "subtype", "derived")


def detect(path: Path) -> tuple[str, str]:
    """Возвращает (банк, идентификатор источника) или ("", "") для незнакомого файла."""
    with pdfplumber.open(path) as pdf:
        text = pdf.pages[0].extract_text() or ""
    if re.search(r"АЛЬФА[\s-]*БАНК", text, re.IGNORECASE):
        m = re.search(r"Номер счета\s+(\d+)", text)
        return "alfa", f"alfa_{m[1][-4:] if m else '0000'}"
    if re.search(r"Сбер", text, re.IGNORECASE) and "Расшифровка операций" in text:
        m = re.search(r"••\s*(\d{4})", text) or re.search(r"Номер счёта\s+([\d ]+)", text)
        last4 = re.sub(r"\D", "", m[1])[-4:] if m else "0000"
        return "sber", f"sber_{last4}"
    return "", ""


def dedupe(txns: list[nz.Txn]) -> list[nz.Txn]:
    """Одинаковые строки внутри одного файла (две реальные операции) получают порядковый номер."""
    seen: Counter = Counter()
    for t in txns:
        seen[t.fingerprint] += 1
        if seen[t.fingerprint] > 1:
            t.fingerprint = f"{t.fingerprint}#{seen[t.fingerprint]}"
    return txns


def load_file(path: Path) -> tuple[str, list[nz.Txn], list[str], bool]:
    """Разбирает один PDF. Возвращает (источник, операции, сообщения проверок, сошлись ли итоги)."""
    bank, source = detect(path)
    if not bank:
        return "", [], ["не похоже на выписку Сбера или Альфа-Банка"], False
    msgs: list[str] = []
    if bank == "sber":
        ops, hdr = sber.parse(str(path))
        txns = dedupe(nz.from_sber(ops, source))
        if hdr["opening"] is None or hdr["closing"] is None:
            return source, txns, ["не найдены остатки в шапке выписки"], False
        base = sum(t.amount_kop for t in txns if not t.derived)
        interest = -sum(t.amount_kop for t in txns if t.derived)
        expected = nz.kop(hdr["closing"] - hdr["opening"]) + interest
        ok = base == expected
        msgs.append(f"сверка остатков: операции {base / 100:,.2f}, по шапке {expected / 100:,.2f}")
    else:
        ops, holds, hdr = alfa.parse(str(path))
        txns = dedupe(nz.from_alfa(ops, source))
        if hdr["opening"] is None or hdr["closing"] is None:
            return source, txns, ["не найдены остатки в шапке выписки"], False
        total = sum(t.amount_kop for t in txns)
        expected = nz.kop(hdr["closing"] - hdr["opening"]) + nz.kop(-sum(holds, Decimal(0)))
        ok = total == expected
        msgs.append(f"сверка остатков: операции {total / 100:,.2f}, по шапке {expected / 100:,.2f}")
    unknown = sum(1 for t in txns if t.type == "unknown")
    if unknown:
        msgs.append(f"нераспознанных строк: {unknown}")
    return source, txns, msgs, ok


def write(conn: sqlite3.Connection, txns: list[nz.Txn]) -> tuple[int, int]:
    """Записывает операции. Возвращает (добавлено новых, обновлено существующих)."""
    conn.executescript(TRANSACTIONS_SCHEMA)
    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.executemany(
        f"INSERT INTO transactions ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))}) "
        "ON CONFLICT(fingerprint) DO UPDATE SET type=excluded.type, category=excluded.category, "
        "merchant=excluded.merchant, mcc=excluded.mcc, bank_category=excluded.bank_category, "
        "counterparty=excluded.counterparty, subtype=excluded.subtype",
        [(t.source, t.fingerprint, t.date.isoformat(), t.posted.isoformat() if t.posted else None, t.amount_kop,
          t.type, t.category, t.merchant, t.description, t.mcc, t.bank_category, t.counterparty, t.card_last4,
          t.subtype, t.derived) for t in txns],
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    return after - before, len(txns) - (after - before)


def backup(db_path: str) -> str | None:
    p = Path(db_path)
    if not p.is_file() or p.stat().st_size == 0:
        return None
    dst = f"{db_path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(p, dst)
    return dst


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Импорт PDF-выписок Сбера и Альфа-Банка в базу бота")
    ap.add_argument("folder", help="папка с PDF-выписками (или один PDF)")
    ap.add_argument("--db", default=os.getenv("DB_PATH", "finance.db"), help="путь к базе (по умолчанию DB_PATH или finance.db)")
    ap.add_argument("--force", action="store_true", help="загружать файлы, даже если итоги не сошлись")
    ap.add_argument("--no-backup", action="store_true", help="не делать копию базы перед записью")
    args = ap.parse_args(argv)

    target = Path(args.folder)
    files = [target] if target.is_file() else sorted(target.glob("*.pdf"))
    if not files:
        print(f"В {target} нет PDF-файлов.")
        return 1

    loaded: list[tuple[Path, str, list[nz.Txn]]] = []
    failed = 0
    for f in files:
        source, txns, msgs, ok = load_file(f)
        status = "OK " if ok else "ОШИБКА"
        print(f"[{status}] {f.name}: {source or '?'}, операций {len(txns)}")
        for m in msgs:
            print(f"        {m}")
        if ok or (args.force and txns):
            loaded.append((f, source, txns))
        else:
            failed += 1
            print("        файл пропущен (используйте --force, чтобы загрузить его несмотря на расхождение)")

    if not loaded:
        print("Нечего загружать.")
        return 1

    if not args.no_backup:
        b = backup(args.db)
        if b:
            print(f"Копия базы: {b}")
    conn = sqlite3.connect(args.db)
    try:
        from .manual import sync_manual
        added = sync_manual(conn)
        if added:
            print(f"Ручные траты из бота скопированы в общую таблицу: {added}")
        for f, source, txns in loaded:
            new, upd = write(conn, txns)
            print(f"{f.name}: добавлено {new}, обновлено {upd}")
        from .screenshot import drop_screenshot_dupes
        n = drop_screenshot_dupes(conn)
        if n:
            print(f"Помечено {n} ручных/скриншотных операций как дубли: они теперь есть в выписках.")
    finally:
        conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
