# GhostLoc

Service payant qui change la position GPS d'un iPhone (carte d'amis Snapchat, Plans, Life360…), sur le modèle de liveloc.io.

| Élément | Dossier | Rôle |
|---|---|---|
| Site | `index.html`, `compte.html`, `installation.html`, `legal.html`, `assets/` | Vitrine, comptes, paiement, téléchargement |
| Configuration | `config.json` | Adresse Supabase, clé publique, URL du site, liens de téléchargement (lue par le site **et** embarquée dans l'app PC) |
| Base + quota | `supabase/migrations/` | Abonnements, packs d'heures, décompte du temps (fonctions SQL `gl_*`) |
| Paiement | `supabase/functions/` | `gl-checkout` (Stripe Checkout, changement de formule, réactivation), `gl-portal` (résiliation, factures), `gl-stripe-webhook` ; tests `*/handler.test.ts` |
| App PC | `desktop/` | Pilote l'iPhone (pymobiledevice3), interface web locale, pilotage depuis l'iPhone par QR code |

## Formules

- **Essentiel** 9,99 €/mois : 48 h de position par mois. Packs 10 h (2,99 €) et 25 h (5,99 €), réservés aux abonnés Essentiel, valables jusqu'à la fin de la période en cours.
- **Illimité** 29,99 €/mois : sans quota, trajets A → B (marche, vélo, voiture sur routes réelles, avion), vitesse réglable.

Les prix Stripe sont créés automatiquement au premier paiement (clés `gl_essentiel`, `gl_illimite`, `gl_pack10`, `gl_pack25`). Pour changer un prix : modifie `CATALOG` dans `supabase/functions/_shared/stripe.ts` **et** archive l'ancien prix dans Stripe (sinon l'ancien `lookup_key` reste utilisé).

## Comment la position est changée

L'app PC parle à l'iPhone comme Xcode : appairage (« Se fier »), mode développeur, image développeur montée automatiquement, puis service Apple de simulation de position.

- iOS 17.4+ : tunnel en espace utilisateur (aucun droit administrateur), service DVT `LocationSimulation`. La position simulée n'existe que tant que l'app reste connectée : fermer l'app ou débrancher l'iPhone rétablit la vraie position.
- iOS 16 et moins : service `com.apple.dt.simulatelocation`.
- iOS 17.0 à 17.3 : non pris en charge (message demandant la mise à jour).

Le temps est décompté côté serveur : `gl_session_start` à la première téléportation, `gl_session_heartbeat` toutes les 60 s (120 s max comptées par intervalle), `gl_session_stop` au retour réel. Quota épuisé, abonnement terminé ou 3 signes de vie perdus d'affilée : l'app rétablit la vraie position.

## Mise en ligne (≈ 30 minutes)

### 1. Supabase
1. Crée un projet dédié sur supabase.com (région UE).
2. SQL Editor : exécute `supabase/migrations/20261010000000_ghostloc.sql`.
3. Authentication › URL Configuration :
   - Site URL : `https://fayti.github.io/cotea-prototype/ghostloc`
   - Redirect URLs : `https://fayti.github.io/cotea-prototype/ghostloc/**` (couvre la confirmation d'e-mail, avec ou sans `?plan=…`, et la réinitialisation du mot de passe)
4. Authentication › Emails : laisse la confirmation d'e-mail activée ; pour la production, branche un SMTP (Resend, Brevo…) car l'envoi intégré est limité à quelques e-mails par heure.
5. Project Settings › API : copie l'URL du projet et la clé `anon` dans `config.json`, avec ton e-mail de support.

### 2. Stripe
1. Récupère la clé secrète (`sk_test_…` pour tester, `sk_live_…` ensuite).
2. Depuis le dossier `ghostloc/` :
   ```bash
   supabase login
   supabase secrets set --project-ref <REF> STRIPE_SECRET_KEY=sk_test_xxx SITE_URL=https://fayti.github.io/cotea-prototype/ghostloc
   supabase functions deploy gl-checkout gl-portal --project-ref <REF>
   supabase functions deploy gl-stripe-webhook --no-verify-jwt --project-ref <REF>
   ```
3. Stripe › Développeurs › Webhooks › Ajouter un endpoint : `https://<REF>.supabase.co/functions/v1/gl-stripe-webhook`, événements `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.paid`.
4. Copie le secret de signature (`whsec_…`) : `supabase secrets set --project-ref <REF> STRIPE_WEBHOOK_SECRET=whsec_xxx`.

Le portail client Stripe (résiliation, factures, carte) est configuré automatiquement au premier clic sur « Gérer / résilier ».

### 3. Site
Le workflow `pages.yml` publie la racine du dépôt à chaque push sur `main` : le site est servi à `https://fayti.github.io/cotea-prototype/ghostloc/`. Pour un nom de domaine à toi, héberge le dossier `ghostloc/` (sans `desktop/` ni `supabase/`) sur Netlify, Vercel ou Cloudflare Pages, puis remplace `siteUrl`, `SITE_URL` et les URL de redirection Supabase.

### 4. App PC
`config.json` est embarqué au moment du build : remplis-le **avant**. Puis :
```bash
git tag ghostloc-v1.0.0 && git push origin ghostloc-v1.0.0
```
Pour chaque nouvelle version : augmente `__version__` dans `desktop/ghostloc_desktop/__init__.py`, publie le tag, puis mets le même numéro dans `version.json` (sur `main`). Les apps déjà installées affichent alors « Nouvelle version » avec un lien vers la page d'installation.
Le workflow `ghostloc.yml` teste tout, construit `GhostLoc-Windows.zip` et `GhostLoc-macOS.zip` (Apple Silicon), vérifie chaque exécutable (`--selftest`) et publie une release GitHub. Les liens de `config.json › downloads` pointent vers la dernière release.

### 5. Vérifier
1. Crée un compte sur le site, choisis Essentiel, paie avec la carte de test `4242 4242 4242 4242` (date future, CVC quelconque).
2. « Mon compte » doit afficher Essentiel actif et 48 h 00 restantes.
3. Lance l'app PC, connecte-toi, branche un iPhone, téléporte-toi : Plans doit afficher la ville choisie.
4. Après quelques minutes, « Dernières utilisations » affiche la session et le compteur baisse.
5. Passe à l'Illimité depuis le compte : le prorata est facturé, les trajets se débloquent dans l'app (bouton ↻).

### 6. Avant d'encaisser de vrais paiements
- Complète les champs `[À COMPLÉTER]` de `legal.html` (éditeur, SIREN, médiateur).
- Passe Stripe en mode live (nouvelle clé secrète + nouveau webhook).
- Fonds de carte : `tile.openstreetmap.org` convient pour démarrer mais interdit l'usage intensif ; au-delà de quelques centaines d'utilisateurs, prends une clé MapTiler ou Stadia et change l'URL des tuiles dans `index.html` et `desktop/ghostloc_desktop/ui/index.html`.
- Signature de code : sans certificat, Windows SmartScreen et macOS Gatekeeper affichent un avertissement (contournement expliqué dans `installation.html`). Un certificat Windows (~200 €/an) et un compte Apple Developer (99 €/an, signature + notarisation) le suppriment.

## Développement

```bash
cd desktop
pip install -r requirements.txt pytest httpx
python -m pytest -q tests                 # 14 tests
python -m ghostloc_desktop --demo         # iPhone simulé, interface sur http://127.0.0.1:8765
python build.py                           # exécutable dans dist/

# Base : PostgreSQL 16 nu
psql -f supabase/tests/auth_stub.sql -f supabase/migrations/20261010000000_ghostloc.sql -f supabase/tests/quota_test.sql
```

Variables utiles : `GHOSTLOC_SUPABASE_URL`, `GHOSTLOC_SUPABASE_ANON_KEY`, `GHOSTLOC_SITE_URL` (remplacent `config.json`). Le bouton « Copier le diagnostic » de l'app donne au support les versions, l'état de l'iPhone et la fin du journal (`ghostloc.log` dans `%APPDATA%\GhostLoc`, `~/Library/Application Support/GhostLoc` ou `~/.config/ghostloc`). Options : `--port`, `--local-only` (refuse le pilotage depuis le Wi-Fi), `--no-browser`, `--selftest`.

## Limites connues

- L'ordinateur doit rester allumé pendant l'utilisation. Une app iPhone autonome (comme LiveLoc) demanderait un développement Swift distinct, signé et réinstallé tous les 7 jours avec un compte Apple gratuit.
- Snapchat et les autres apps peuvent détecter une simulation ; leurs conditions d'utilisation l'interdisent et un compte peut être restreint.
- Wi-Fi sans câble : expérimental, dépend de la synchronisation Wi-Fi d'Apple Devices/Finder.
- macOS Intel : non construit par défaut (ajouter un runner Intel à la matrice du workflow).
