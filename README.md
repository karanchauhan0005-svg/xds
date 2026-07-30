# Apka Munim 💰

A Hinglish-first personal finance tracker built with **React + FastAPI + MongoDB**.

Track income, expenses, udhaar (loans), multiple accounts (Savings/Current/Cash), budgets, recurring transactions, and family/shared ledgers — all with AI-powered insights and a warm Hinglish tone.

## Features

- Multi-currency (INR default + USD/EUR/GBP/AED)
- Multiple accounts with live balance tracking
- Income / Expense (Aaya / Gaya) with 17+ categories
- Udhaar tracker (Lene / Dene) with WhatsApp reminder
- Budget goals per category with breach notifications
- Recurring transactions (daily/weekly/monthly)
- Family / Shared ledgers with invite codes
- CSV / PDF monthly export
- AI-powered insights via Claude Sonnet 4.5 (Anthropic)
- UPI/Bank SMS parser (regex + LLM fallback)
- Dark mode
- PWA installable (Android home screen / iOS "Add to Home Screen")
- Data export (JSON) and account delete for GDPR / Play Store compliance

## Quick Start (Local Development)

```bash
# 1. Backend
cd backend
pip install -r requirements.txt
cp ../.env.example .env    # then edit .env
uvicorn server:app --reload --port 8001

# 2. Frontend (separate terminal)
cd frontend
yarn install
cp ../.env.example .env     # then edit REACT_APP_BACKEND_URL
yarn start
```

## Self-Hosting

See **[DEPLOYMENT.md](./DEPLOYMENT.md)** for step-by-step guides on:

- **Path A:** MongoDB Atlas + Railway + Vercel (~$5/mo, recommended)
- **Path B:** MongoDB Atlas + Render (free) + Vercel (backend sleeps)
- **Path C:** Any VPS with Docker Compose (`docker compose up -d`)

## Environment Variables

See [.env.example](./.env.example). Key ones:

| Variable | Where | Purpose |
|---|---|---|
| `MONGO_URL` | backend | MongoDB connection string |
| `JWT_SECRET` | backend | 64-char random hex for JWT signing |
| `CORS_ORIGINS` | backend | Comma-separated allowed frontend URLs |
| `ANTHROPIC_API_KEY` | backend | Direct Anthropic key for LLM (self-hosted) |
| `EMERGENT_LLM_KEY` | backend | Emergent-only LLM key (auto-set on Emergent) |
| `REACT_APP_BACKEND_URL` | frontend | Public backend URL |

## License

MIT — you own the code. See `LICENSE` file.

## Credits

Built with ♥ on the [Emergent](https://emergent.sh) platform. Portable to any host.
