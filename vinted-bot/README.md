# Vinted Bot — alertes en quasi temps réel

Surveille des recherches Vinted et envoie une alerte (Telegram, Discord, console) quelques
secondes après la publication d'une annonce, avec un indicateur « bonne affaire » calculé
contre la médiane des prix récents de la même recherche.

## Comment ça marche

- **Source** : Vinted n'a ni API publique ni flux temps réel. Le bot interroge l'endpoint
  interne `/api/v2/catalog/items?order=newest_first`, celui qu'utilise le site.
- **Latence** = intervalle de polling (5 s par défaut) + durée de la requête (≈ 0,3 à 1 s) +
  envoi Telegram (≈ 0,3 s). On peut donc compter sur 2 à 7 s en moyenne.
- **Détection des nouveautés** : les ID d'annonce sont croissants. Le bot garde le plus grand
  ID vu (le « filigrane ») pour chaque recherche. Une annonce remontée ou sponsorisée garde son
  ancien ID et n'est donc pas signalée. Le filigrane est persisté en SQLite : un redémarrage ne
  renvoie pas d'anciennes alertes.
- **Premier lancement** : 96 annonces sont chargées comme référence pour la médiane, sans
  alerte.
- **Bonne affaire** : `1 - prix / médiane(200 derniers prix de la recherche) ≥ 30 %`.
- **Anti-blocage** : curl_cffi imite l'empreinte TLS de Chrome. Le cookie de session est
  renouvelé automatiquement (401). Un écart minimal entre requêtes est appliqué par domaine.
  En cas de 403/429, le bot recule de façon exponentielle (30 s → 10 min) et passe au proxy
  suivant.

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
| `interval` | intervalle propre à cette recherche |

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

- **Débit total** ≈ `nombre de recherches / interval` requêtes par seconde, borné par
  `min_request_gap`. Avec 5 recherches, `interval = 5` et `min_request_gap = 1`, on envoie
  1 requête par seconde, ce qui est déjà soutenu pour une seule IP.
- Si des **403/429** apparaissent dans les logs, augmentez `interval` ou `min_request_gap`,
  ou ajoutez des `proxies` résidentiels. Les IP de datacenter (VPS) sont bloquées bien plus
  vite qu'une connexion domestique.
- Préférez des **recherches précises** (catégorie + taille + prix max) à une recherche large.
  Une recherche large reçoit plus de `per_page` nouveautés entre deux requêtes et en rate
  (le bot l'indique dans les logs).
- Ce qui compte vraiment pour la rapidité, c'est d'acheter ou de faire une offre en un clic
  depuis l'application dès l'alerte Telegram reçue.

## Limites

- L'API utilisée n'est pas documentée : Vinted peut la modifier ou durcir son anti-bot à tout
  moment. L'utilisation automatisée est contraire aux conditions d'utilisation de Vinted et
  peut entraîner une restriction de l'IP ou du compte. Le bot ne se connecte à aucun compte et
  n'achète rien : il se contente de lire les recherches publiques.
- La médiane est calculée sur les prix de la recherche elle-même. Une recherche hétérogène
  (ex. « nike ») donne une médiane peu significative. Plus la recherche est précise, plus le
  signal « bonne affaire » est fiable.
