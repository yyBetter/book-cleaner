.PHONY: setup serve test demo check-js

setup:
	uv sync --python 3.12

serve:
	uv run book-cleaner serve --host 0.0.0.0 --port 8765

test:
	uv run pytest -v

demo:
	uv run python scripts/generate_demo_book.py --out-dir ./data/demo_scans

check-js:
	node --check book_cleaner/web/static/*.js
