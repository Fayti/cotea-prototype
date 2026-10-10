# Vinted Bot — alertes en quasi temps réel

Surveille des recherches Vinted et envoie une alerte (Telegram, Discord, console) quelques
secondes après la publication d'une annonce, avec un indicateur « bonne affaire » calculé
contre la médiane des prix récents de la même recherche.

## Comment ça marche

- **Source** : Vinted n'a ni API publique ni flux temps réel. Le bot interroge l'API interne
  du site, `https://api.vinted.fr/svc-catalogue/items?order=newest_first`, avec le jeton
  anonyme que le site pose dans le cookie `access_token_web`. Cette adresse remplace
  `/api/v2/catalog/items` depuis septembre 2026. Les filtres catégorie, marque, taille, état,
  couleur et matière passent désormais par `attribute_ids[...]`. Le bot convertit
  automatiquement les liens copiés depuis le site.
- **Délai d'alerte** = délai d'indexation chez Vinted (incompressible, inconnu, probablement
  quelques secondes) + attente du prochain passage sur la recherche + requête (≈ 0,2 à 1 s) +
  envoi Telegram (≈ 0,3 s).
- **Ordonnanceur** : le bot envoie une requête dès que le rythme autorisé le permet, en
  tournant sur les recherches, et sans attendre la réponse précédente : la lenteur de Vinted
  ne ralentit pas la cadence. Temps entre deux passages sur une recherche ≈
  `request_gap × nombre de recherches / nombre d'IP`. Avec 1 IP, `request_gap = 1` et 3
  recherches : un passage toutes les 3 s, soit ≈ 1,5 s d'attente moyenne.
- **Détection des nouveautés** : une annonce est nouvelle si elle n'a jamais été vue dans
  cette recherche **et** qu'elle est récente. Les numéros d'annonce ne suivent pas l'ordre
  d'apparition : mesuré sur le vrai Vinted, 43 % des nouveautés arrivent avec un numéro
  inférieur au plus grand déjà vu, car Vinted indexe avec un léger décalage. Un simple
  « numéro > dernier vu » en raterait donc près de la moitié. La condition de récence écarte
  les annonces anciennes remontées, sponsorisées, ou révélées quand une annonce au-dessus est
  vendue. Tout est persisté en SQLite : un redémarrage ne renvoie pas d'anciennes alertes.
- **Âge des annonces** : Vinted ne renvoie plus de date de publication. Les numéros sont
  attribués au rythme d'environ 15 000 par minute. Toutes les 30 s, le bot relève le numéro
  le plus récent du catalogue et en déduit l'âge de chaque annonce (« repérée ~8 s après sa
  mise en ligne »), à quelques secondes près.
- **Premier lancement** : 96 annonces sont chargées comme référence pour la médiane, sans
  alerte.
- **Bonne affaire** : `1 - prix / médiane(200 derniers prix de la recherche) ≥ 30 %`.
- **Anti-blocage, auto-réglé** : curl_cffi imite l'empreinte TLS de Chrome, et le cookie de
  session est renouvelé automatiquement (401). Chaque IP (connexion directe ou proxy) a son
  propre rythme. Après un 403/429, la voie se met en pause (30 s, puis 60 s, … jusqu'à
  10 min), change de cookies et ralentit (écart × 1,5). À chaque succès, elle se rapproche
  ensuite de `request_gap`. Le bot converge ainsi vers le rythme le plus rapide que Vinted
  tolère, et les autres IP continuent pendant la pause.
- **Telegram** : `sendMessage` avec la photo en aperçu. Ce message part plus vite qu'un
  `sendPhoto`, qui oblige Telegram à télécharger l'image d'abord.

## Installation sur Mac

1. Ouvre le **Terminal** : appuie sur Cmd + Espace, tape « Terminal », puis Entrée.
2. Colle cette ligne puis appuie sur Entrée :

   ```bash
   curl -fsSL https://github.com/Fayti/cotea-prototype/releases/download/vinted-bot-latest/install-mac.sh | bash
   ```

3. Le script détecte ton type de Mac (puce Apple ou Intel), télécharge le bot dans le dossier
   `VintedBot` de ton dossier utilisateur, pose un raccourci **VintedBot** sur le Bureau,
   puis lance le bot. L'interface s'ouvre dans ton navigateur (http://127.0.0.1:8765).
4. Laisse la fenêtre du Terminal ouverte : la fermer arrête le bot. Pour le relancer plus
   tard, double-clique sur **VintedBot** sur le Bureau.

Le bot empêche la mise en veille automatique tant qu'il tourne (`caffeinate`). En revanche,
un MacBook dont on ferme le capot s'endort quand même, sauf s'il est branché sur secteur et
relié à un écran externe.

Pourquoi le Terminal plutôt qu'un téléchargement dans le navigateur ? macOS bloque les
programmes non signés téléchargés par un navigateur (Gatekeeper), et depuis macOS 15 le
contournement passe par plusieurs écrans des Réglages. Un fichier téléchargé par `curl`
n'est pas marqué ainsi. La signature Apple coûte 99 $ par an.

## Installation sur Windows (sans rien installer)

1. Télécharge **VintedBot.exe** :
   https://github.com/Fayti/cotea-prototype/releases/download/vinted-bot-latest/VintedBot.exe
2. Double-clique dessus. Windows peut afficher « Windows a protégé votre ordinateur » : le
   fichier n'est pas signé (une signature coûte plusieurs centaines d'euros par an). Clique
   sur « Informations complémentaires », puis « Exécuter quand même ».
3. Une fenêtre noire s'ouvre (laisse-la ouverte : la fermer arrête le bot) et l'interface
   s'affiche dans ton navigateur à l'adresse http://127.0.0.1:8765.
4. Dans l'interface : **Recherches → Ajouter**, colle le lien d'une recherche Vinted, puis
   **Réglages → Telegram** pour recevoir les alertes sur ton téléphone.

Les réglages et l'historique sont enregistrés dans le dossier `VintedBot` de ton dossier
utilisateur. Relancer le .exe reprend là où le bot s'était arrêté.

L'exécutable est construit et testé automatiquement par GitHub Actions
(`.github/workflows/vinted-bot.yml`) à chaque modification du dossier `vinted-bot/`.

## Depuis le code source (Linux, macOS, Windows)

```bash
cd vinted-bot
python3 -m venv .venv && source .venv/bin/activate   # Python ≥ 3.11
pip install -r requirements.txt
python -m vinted_bot              # interface dans le navigateur
```

Mode ligne de commande, sans interface, avec un fichier de configuration :

```bash
cp config.example.toml config.toml   # puis éditer
python -m vinted_bot check -c config.toml        # une requête par recherche : vérifie l'accès et les filtres
python -m vinted_bot test-notify -c config.toml  # alerte factice sur chaque canal
python -m vinted_bot run -c config.toml          # surveillance continue
python -m pytest -q tests                        # tests
python scripts/probe_vinted.py                   # sonde le vrai Vinted (accès, format, latence)
```

Dans `config.toml`, la section `[[search]]` accepte :

| Clé | Effet |
|---|---|
| `url` | lien de recherche copié depuis vinted.fr (filtres appliqués côté serveur) |
| `exclude` | rejette si le titre contient un mot commençant ainsi (accents ignorés) |
| `include_any` | exige au moins un de ces mots dans le titre |
| `max_price` / `min_price` | bornes de prix (`max_price` est aussi envoyé à l'API) |
| `exclude_sellers` | logins ignorés |
| `only_deals` | n'alerte que les annonces sous le seuil de médiane |
| `weight` | fréquence relative de passage (2 = deux fois plus souvent) |

## Réglage de la vitesse

Toutes les 60 s, le bot écrit une ligne de stats dans les logs :

```
Stats : 0.98 req/s · réponse Vinted médiane 310 ms · passage toutes les : Air Max 2.0 s, Carhartt 4.1 s · 1 alerte(s), âge photo médian 47 s
```

- **Le seul vrai levier est le nombre d'IP.** Le débit par IP est plafonné par l'anti-bot de
  Vinted. Chaque proxy de `proxies` ajoute une voie complète : 3 IP = 3× plus de passages. Les
  proxies résidentiels ou mobiles tiennent beaucoup mieux que les IP de datacenter.
- **Moins de recherches = plus rapide.** Le débit est partagé entre toutes les recherches.
  Fusionnez ou supprimez les recherches secondaires, et mettez `weight = 3` sur celles qui
  comptent vraiment.
- **`request_gap`** est le plancher. En dessous de ~0,5 s par IP, attendez-vous à des
  blocages fréquents : chaque pause coûte au moins 30 s sur cette voie. Si la ligne de stats
  montre des blocages réguliers, remontez `request_gap` jusqu'à ce qu'ils disparaissent.
  Une voie qui ne se bloque jamais va plus vite qu'une voie qui oscille.
- **Recherches précises** (catégorie + taille + prix max) : si plus de `per_page` nouveautés
  arrivent entre deux passages, certaines sont ratées (le bot l'indique dans les logs).
- **« âge photo »** mesure le temps depuis l'envoi de la photo principale. Le vendeur ajoute
  souvent ses photos une à plusieurs minutes avant de publier : cette valeur majore donc le
  vrai délai de détection. Elle sert à comparer des réglages entre eux, pas à mesurer le
  délai absolu.
- Une fois l'alerte reçue, ce qui fait la différence, c'est d'acheter ou de faire une offre
  en un clic depuis l'application Vinted.

## Limites

- Le catalogue renvoyé dépend du pays de l'adresse IP. Depuis une IP américaine (serveurs de
  GitHub), Vinted renvoie des annonces américaines en dollars. Le bot doit donc tourner depuis
  une connexion française, ou derrière des proxies français.

- L'API utilisée n'est pas documentée : Vinted peut la modifier ou durcir son anti-bot à tout
  moment. L'utilisation automatisée est contraire aux conditions d'utilisation de Vinted et
  peut entraîner une restriction de l'IP ou du compte. Le bot ne se connecte à aucun compte et
  n'achète rien : il se contente de lire les recherches publiques.
- La médiane est calculée sur les prix de la recherche elle-même. Une recherche hétérogène
  (ex. « nike ») donne une médiane peu significative. Plus la recherche est précise, plus le
  signal « bonne affaire » est fiable.
