name: Price targets to Telegram

on:
  schedule:
    - cron: "*/15 * * * *"   # every 15 minutes
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: targets-bot          # own queue, does not wait for the news bot
  cancel-in-progress: false

jobs:
  post-targets:
    runs-on: ubuntu-latest
    timeout-minutes: 50
    steps:
      - uses: actions/checkout@v5

      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"

      - name: Install
        run: pip install yfinance

      - name: Post price targets
        run: python targets.py
        env:
          TARGETS_BOT_TOKEN: ${{ secrets.TARGETS_BOT_TOKEN }}
          TARGETS_CHAT_ID: ${{ secrets.TARGETS_CHAT_ID }}
          TARGETS_THREAD_ID: ${{ secrets.TARGETS_THREAD_ID }}

      - name: Remember checked stocks
        if: always()
        run: |
          git config user.name "news-bot"
          git config user.email "news-bot@users.noreply.github.com"
          git add targets_state.json || true
          if ! git diff --cached --quiet; then
            git commit -m "Update price target state"
            git pull --rebase
            git push
          fi
