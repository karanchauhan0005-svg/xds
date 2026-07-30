# Apka Munim — Self-Hosting Guide

Apka Munim ka code fully portable hai. Ye guide 3 sabse asaan paths dikhati hai — beginner-friendly hai.

---

## Kya milega isme?

- **Path A:** MongoDB Atlas + Railway + Vercel  → **~$5/month** (simplest)
- **Path B:** MongoDB Atlas + Render (free) + Vercel  → **~$0-$1/month** (backend sleeps on free plan)
- **Path C:** VPS + Docker Compose  → **$5-$10/month** (advanced, but full control)

---

## Prerequisites (ek baar setup)

1. **GitHub account** — code repo host karne ke liye
2. **Domain** (optional) — `kawachine.com` ya koi bhi
3. **Credit card** — most platforms free tier ke liye bhi verify mangte hain (koi charge nahi hoga)

---

## STEP 1 — Code GitHub pe push karo

Emergent chat mein **"Save to GitHub"** button dabao aur naya repo `apka-munim` create karo. Baad mein har change auto-sync hoga.

---

## STEP 2 — MongoDB Atlas (database) — free forever

1. https://cloud.mongodb.com pe signup karo (Google login se easy)
2. **Build a Database** → **M0 (FREE)** → **AWS Mumbai** region
3. **Database Access** → naya user banao: username `apkamunim`, password strong hona chahiye
4. **Network Access** → **Add IP** → `0.0.0.0/0` (baad mein hosting IP se restrict karlena)
5. **Connect** → **Drivers** → connection string copy karo:
   ```
   mongodb+srv://apkamunim:<pass>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
   ```
   `<pass>` ki jagah aapka actual password lagao (URL-encoded karo agar special chars ho)

6. **Data migrate karo** (Emergent chat mein bolo main `mongodump` archive bana ke share kar dunga; ya SSH access hai to):
   ```bash
   # Emergent side pe (main karta hoon):
   mongodump --uri="$MONGO_URL" --archive=/tmp/dump.gz --gzip

   # Local machine pe:
   mongorestore --uri="mongodb+srv://apkamunim:<pass>@cluster0..." \
                --archive=./dump.gz --gzip
   ```

---

## STEP 3 — LLM Key (Anthropic)

Direct Anthropic key lo — Emergent LLM Key bahar kaam nahi karega.

1. https://console.anthropic.com signup
2. **Settings → Billing** → $5 add karo (chalega ~2-3 mahine)
3. **API Keys → Create Key** → copy `sk-ant-api03-...`
4. Save karo, dobara nahi milegi

**Free alternative:** Groq (Llama 3.3, free) — thoda code change chahiye (bata dena, main patch bana dunga).

---

## PATH A — Railway (backend) + Vercel (frontend) — Recommended

### 3A. Backend deploy on Railway

1. https://railway.app signup with GitHub
2. **New Project → Deploy from GitHub repo** → apka-munim repo select karo
3. **Add Service → From repo** → detect ho jayega
4. **Settings → Root Directory:** `backend`
5. **Settings → Build:** Dockerfile auto-detect ho jayegi
6. **Variables** tab, ye add karo:
   ```
   MONGO_URL=mongodb+srv://apkamunim:...        (Atlas connection string)
   DB_NAME=paisabook_db
   JWT_SECRET=<64-char-random-hex>              (generate: `openssl rand -hex 32`)
   CORS_ORIGINS=https://apka-munim.vercel.app,https://kawachine.com
   ANTHROPIC_API_KEY=sk-ant-api03-...
   ```
7. **Settings → Networking → Generate Domain** → mile jayega:
   `https://apka-munim-backend-production.up.railway.app`
8. **Verify:** `curl https://<url>/api/` → `{"app":"Apka Munim","status":"ok"}`

**Cost:** $5/month starter plan (small usage).

### 3A. Frontend deploy on Vercel

1. https://vercel.com signup with GitHub
2. **Add New → Project** → apka-munim repo select karo
3. **Root Directory:** `frontend`
4. **Framework Preset:** Create React App (auto-detect)
5. **Environment Variables:**
   ```
   REACT_APP_BACKEND_URL=https://apka-munim-backend-production.up.railway.app
   ```
6. **Deploy** → 2 min mein `https://apka-munim.vercel.app` live

**Cost:** FREE (Hobby plan is plenty for personal apps).

### 3A. Custom domain (optional)

- **Vercel:** Project → Settings → Domains → Add `kawachine.com` → DNS records copy karo → apne registrar (GoDaddy/Cloudflare) mein add karo → 10 min mein SSL auto-issue ho jayega
- **Emergent DNS records hata do** so both don't conflict

---

## PATH B — Render (backend, free) + Vercel (frontend)

Same as Path A, except backend Render pe:

1. https://render.com signup with GitHub
2. **New → Blueprint** → repo select → `render.yaml` auto-detect ho jayegi
3. Missing env vars fill karo (`MONGO_URL`, `ANTHROPIC_API_KEY`, `CORS_ORIGINS`)
4. Deploy click → 4-5 min

**⚠️ Free tier catch:** 15 min inactivity ke baad backend so jata hai. First request pe 30 sec cold-start hoga.

---

## PATH C — VPS with Docker Compose (advanced)

Agar aapke paas DigitalOcean/Linode/Hetzner VPS hai:

```bash
# On the VPS:
git clone https://github.com/<you>/apka-munim.git
cd apka-munim

# Create .env file with all secrets (see .env.example)
cp .env.example .env
nano .env         # fill in JWT_SECRET, ANTHROPIC_API_KEY, etc

# Deploy
docker compose up -d

# Frontend at http://<vps-ip>/
# Backend at http://<vps-ip>:8001/api/
```

Nginx reverse proxy + Let's Encrypt SSL alag setup karna hoga. Certbot easy hai.

---

## Post-Deploy Checklist

- [ ] Register a fresh test account on new deployment
- [ ] Login → dashboard loads
- [ ] Add an account → transaction → shows in list
- [ ] SMS parser works → toast "Parse ho gaya"
- [ ] AI insights work (Anthropic key valid)
- [ ] PDF export downloads
- [ ] Dark mode toggles
- [ ] Data export (JSON) downloads
- [ ] Delete-my-account works (test with throwaway user)
- [ ] Original data restored from Atlas (login as `demo@paisabook.com`)

---

## Common Errors

| Error | Fix |
|---|---|
| `CORS blocked` | Backend `CORS_ORIGINS` mein frontend URL add karo, restart |
| `401 unauthorized` after login | Cookie SameSite issue — HTTPS use karo (not http) |
| `Cannot POST /api/...` | Backend URL mein `/api` prefix hai, but Vercel routing chhod raha — verify `REACT_APP_BACKEND_URL` correct hai |
| `MongoNetworkError` | Atlas Network Access mein IP whitelist karo (`0.0.0.0/0` for testing) |
| AI insights fail | `ANTHROPIC_API_KEY` env var set nahi — Railway/Render dashboard mein add karo, restart |
| PWA install button nahi | HTTPS zaruri hai (localhost/127.0.0.1 exception) |

---

## Sab kuch fail ho gaya to?

1. `.env.example` file dekho — koi variable miss to nahi
2. Backend logs check karo — Railway `Logs` tab / Render `Logs` menu / `docker compose logs backend`
3. Frontend browser console mein error dekho
4. GitHub Issues pe post karo aap ka repo mein — mai (via Emergent) help kar sakta hoon

**Success!** Aapka Apka Munim ab **self-hosted** hai. Total time: 30-60 min first time, 5 min subsequent updates (bas `git push` karo).
