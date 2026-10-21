name: News to Telegram

on:
  schedule:
    - cron: "*/10 * * * *"   # every 10 minutes
  workflow_dispatch:          # lets you run it manually with a button

permissions:
  contents: write

concurrency:
  group: news-bot             # own queue, does not wait for the price target bot
  cancel-in-progress: false

jobs:
  post-news:
    runs-on: ubuntu-latest
    timeout-minutes: 50
    steps:
      - uses: actions/checkout@v5

      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"

      - name: Install
        run: pip install -r requirements.txt

      - name: Post news
        run: python bot.py
        env:
          BOT_TOKEN: ${{ secrets.BOT_TOKEN }}
          CHAT_ID: ${{ secrets.CHAT_ID }}
          THREAD_ID: ${{ secrets.THREAD_ID }}

      - name: Remember posted news
        if: always()
        run: |
          git config user.name "news-bot"
          git config user.email "news-bot@users.noreply.github.com"
          git add seen.json || true
          if ! git diff --cached --quiet; then
            git commit -m "Update seen news"
            git pull --rebase
            git push
          fi
