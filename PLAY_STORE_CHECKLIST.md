# 🚀 APKA MUNIM — PLAY STORE SUBMISSION CHECKLIST

## ⚡ Quick Start — 7 Steps to Live

### **STEP 1: Deploy Latest Code (5 min)**
1. GitHub PR merge karo (v15 with billing + Credit Note + Debit Note + Custom Date Filter)
2. Vercel auto-build (2-3 min)
3. Verify **apkamunim.com** live hai with sab features:
   - Global Search (Ctrl+K)
   - Billing Dashboard
   - Credit/Debit Notes
   - Custom Date Filter
   - All 15 features

---

### **STEP 2: Generate AAB File via PWABuilder (10 min) ⭐**

1. Jao: **https://www.pwabuilder.com/**
2. URL daalo: **`https://www.apkamunim.com`**
3. **Start** dabao — PWABuilder aapki PWA scan karega
4. Kuch seconds baad **"Package for Stores"** button dabao
5. **Android** section pe **"Generate Package"** click
6. Options:
   - **Package ID**: `com.apkamunim.app`
   - **App name**: `Apka Munim`
   - **Version**: `1.0.0`
   - **Signing key**: **"Generate new signing key"** select karo
   - **Fill details**:
     - Full name: Aapka naam
     - Organization: Apka Munim
     - Country: IN
     - Password: **STRONG password** (SAVE karo, kal chahiye hoga updates ke liye!)
7. **Download** dabao → ZIP file milegi
8. ZIP extract karo, andar **`app-release-signed.aab`** file milegi — **YE upload karna hai Play Console pe**

---

### **STEP 3: Google Play Console pe Naya App (5 min)**

1. Jao: **https://play.google.com/console**
2. Left panel → **"Create app"** click
3. Fill karo:
   - **App name**: Apka Munim
   - **Default language**: English (India) - en-IN
   - **App or game**: App
   - **Free or paid**: Free
   - ✅ Declarations: Sab check karo
4. **Create app** dabao

---

### **STEP 4: App Content Setup (15 min)**

Left sidebar mein Yeh sabhi ✅ karo:

#### **Privacy Policy**:
- URL: **`https://www.apkamunim.com/privacy`**

#### **App Access**:
- All functionality is available without special access → select

#### **Ads**:
- No, my app does not contain ads (abhi ke liye)

#### **Content Rating** — questionnaire fill karo:
- Category: **Finance**
- Kuch bhi violent/adult content nahi → **Everyone (3+)** rating milegi

#### **Target Audience**:
- Ages: **18 and over**

#### **News App / COVID / Government** — sab NO

#### **Data Safety** — declare karo:
- Data collected: **Financial info** (transactions, budgets)
- Data shared: NO third party
- Encryption: YES in transit + at rest
- User can delete: YES (Settings → Delete Account)

---

### **STEP 5: Store Listing (20 min)**

#### **App Details**:
- **App name**: Apka Munim
- **Short description** (80 chars):
  > Personal + business finance tracker — GST invoices, udhaar, budgets, sab kuch!

- **Full description** (4000 chars): Copy from `/app/PLAY_STORE_LISTING.md`

#### **Graphics** (ye banwane padhenge — Canva ya Photoshop):

| Asset | Size | Purpose |
|-------|------|---------|
| **App Icon** | 512×512 PNG | Main icon (already at `/app/frontend/public/logo512.png`) |
| **Feature Graphic** | 1024×500 PNG | Header banner on store |
| **Phone Screenshots** | 2-8 images, min 320px | Show app screens |
| **Tablet Screenshots** (optional) | 2-8 images | 7-inch + 10-inch |

**Screenshots leni hai in pages ki (apkamunim.com pe login karke)**:
1. Dashboard (with net worth + emergency fund)
2. Add Transaction dialog
3. Billing Dashboard (Aaj ki sale + shortcuts)
4. Invoice Create screen
5. Global Search (Ctrl+K)
6. Reports/Analytics

**Tip**: Chrome DevTools → Device Mode → iPhone 12 Pro select → Screenshot leke apkamunim.com khol ke lo.

---

### **STEP 6: Upload AAB (5 min)**

1. Left sidebar → **Production** → **Create new release**
2. **Upload AAB** section mein **`app-release-signed.aab`** drag-drop karo
3. **Release name**: `1.0.0 - Initial Launch`
4. **Release notes**:
   ```
   🎉 Apka Munim launched!
   • Personal finance tracker (income, expense, udhaar)
   • Business billing (GST invoice, Credit/Debit note, inventory)
   • AI insights (Munim Ji chatbot)
   • Family shared ledgers
   • Voice input, dark mode, offline PWA
   ```
5. **Save** → **Review release** → **Start rollout to production**

---

### **STEP 7: Countries + Pricing (2 min)**

1. **Countries/regions** → **Add countries** → **India** select karo (baaki chaho toh sab)
2. **Pricing** → **Free** (already set)

---

## ✅ Submit for Review

Left sidebar mein **"Send for review"** button dabao. Google **1-3 days** mein review karega. Approved hone ke baad app **live** on Play Store!

---

## 🎨 Assets Ready in Codebase

| File | Location | Purpose |
|------|----------|---------|
| `manifest.json` | `/app/frontend/public/` | PWA config (Play Store reads this) |
| `assetlinks.json` | `/app/frontend/public/.well-known/` | Domain verification |
| `logo192.png`, `logo512.png` | `/app/frontend/public/` | App icons |
| `PLAY_STORE_LISTING.md` | `/app/` | Description + keywords |
| `banners.html` | `/app/frontend/public/` | Feature graphic templates |

---

## ⚠️ Common Mistakes to Avoid:

1. ❌ **Signing key password bhulna** — password lost = future updates impossible
2. ❌ **assetlinks.json wrong** — Google can't verify domain ownership
3. ❌ **Privacy policy URL 404** — check `apkamunim.com/privacy` loads
4. ❌ **Screenshots low quality** — min 320px width, PNG format
5. ❌ **Test app on device first** — sideload AAB → install → verify sab kaam kare
