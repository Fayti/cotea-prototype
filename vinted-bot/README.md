# Vinted Bot — alertes en quasi temps réel

Surveille des recherches Vinted et envoie une alerte (Telegram, Discord, console) quelques
secondes après la publication d'une annonce, avec un indicateur « bonne affaire » calculé
contre la médiane des prix récents de la même recherche.

## Comment ça marche

- **Source** : Vinted n'a ni API publique ni flux temps réel. Le bot interroge l'endpoint
  interne `/api/v2/catalog/items?order=newest_first`, celui qu'utilise le site.
- **Délai d'alerte** = délai d'indexation chez Vinted (incompressible, inconnu, probablement
  quelques secondes) + attente du prochain passage sur la recherche + requête (≈ 0,2 à 1 s) +
  envoi Telegram (≈ 0,3 s).
- **Ordonnanceur** : le bot envoie une requête dès que le rythme autorisé le permet, en
  tournant sur les recherches, et sans attendre la réponse précédente : la lenteur de Vinted
  ne ralentit pas la cadence. Temps entre deux passages sur une recherche ≈
  `request_gap × nombre de recherches / nombre d'IP`. Avec 1 IP, `request_gap = 1` et 3
  recherches : un passage toutes les 3 s, soit ≈ 1,5 s d'attente moyenne.
- **Détection des nouveautés** : les ID d'annonce sont croissants. Le bot garde le plus grand
  ID vu (le « filigrane ») pour chaque recherche. Une annonce remontée ou sponsorisée garde son
  ancien ID et n'est donc pas signalée. Le filigrane est persisté en SQLite : un redémarrage ne
  renvoie pas d'anciennes alertes.
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

## Installation

```bash
cd vinted-bot
python3 -m venv .venv && source .venv/bin/activate   # Python ≥ 3.11
pip install -r requirements.txt
cp config.example.toml config.toml
```

### Alertes Telegram (recommandé : notification push sur le téléphone)

1. Sur Telegram, écrire à `@BotFather`, envoyer `/newbot` et récupérer le **token**.
2. Envoyer un message quelconque à votre nouveau bot.
3. Ouvrir `https://api.telegram.org/bot<TOKEN>/getUpdates` et noter `"chat":{"id": ...}`.
4. Renseigner `[telegram]` dans `config.toml`, ou les variables `TELEGRAM_TOKEN` et
   `TELEGRAM_CHAT_ID`.

Pour Discord : Paramètres du salon → Intégrations → Webhooks → copier l'URL dans `[discord]`.

### Définir les recherches

Faites la recherche sur vinted.fr avec tous vos filtres (catégorie, marque, taille, état,
prix), puis copiez l'URL de la page dans `url` d'une section `[[search]]`. Les filtres de
l'URL sont appliqués côté serveur. Les filtres locaux viennent en plus :

| Clé | Effet |
|---|---|
| `exclude` | rejette si le titre contient un mot commençant ainsi (accents ignorés) |
| `include_any` | exige au moins un de ces mots dans le titre |
| `max_price` / `min_price` | bornes de prix (`max_price` est aussi envoyé à l'API) |
| `exclude_sellers` | logins ignorés |
| `only_deals` | n'alerte que les annonces sous le seuil de médiane |
| `weight` | fréquence relative de passage (2 = deux fois plus souvent) |

## Utilisation

```bash
python -m vinted_bot check -c config.toml        # une requête par recherche : vérifie l'accès et les filtres
python -m vinted_bot test-notify -c config.toml  # envoie une alerte factice sur chaque canal
python -m vinted_bot run -c config.toml          # surveillance continue (Ctrl+C pour arrêter)
python -m pytest -q tests                        # tests
```

Pour tourner 24 h/24, utiliser une machine allumée en permanence (Raspberry Pi, VPS) et un
service systemd ou `tmux`.

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

- L'API utilisée n'est pas documentée : Vinted peut la modifier ou durcir son anti-bot à tout
  moment. L'utilisation automatisée est contraire aux conditions d'utilisation de Vinted et
  peut entraîner une restriction de l'IP ou du compte. Le bot ne se connecte à aucun compte et
  n'achète rien : il se contente de lire les recherches publiques.
- La médiane est calculée sur les prix de la recherche elle-même. Une recherche hétérogène
  (ex. « nike ») donne une médiane peu significative. Plus la recherche est précise, plus le
  signal « bonne affaire » est fiable.
