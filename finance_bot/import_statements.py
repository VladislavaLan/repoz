"""Точка входа: python import_statements.py <папка_с_pdf> [--db finance.db] [--force]"""

import sys

from dotenv import load_dotenv

load_dotenv()  # подхватить DB_PATH и STATEMENTS_CONFIG из .env

from statements.importer import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
