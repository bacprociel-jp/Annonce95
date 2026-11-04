# Annonces 95 (v2) : appartements à vendre dans le Val-d'Oise

Site gratuit, sans compte : le visiteur règle prix et surface, la liste se filtre instantanément.
Les annonces viennent d'une **recherche directe** sur les sites, sans alerte e-mail.

```
GitHub Actions (toutes les 20 min)                 Ton PC (toutes les 30-60 min)
  Bien'ici (API JSON) + PAP (recherche)              Leboncoin + SeLoger (vrai Chrome)
        │                                                    │
        ▼                                                    ▼
  data/annonces.json                          data/annonces_domicile.json
        └────────────────────┬───────────────────────────────┘
                             ▼
                        index.html  (fusionne, dédoublonne, filtre)
```

Pourquoi deux collecteurs : Leboncoin et SeLoger bloquent les serveurs comme GitHub Actions,
mais acceptent en général un vrai navigateur sur une connexion domestique.

## Mise en ligne, étape par étape

### 1. Le dépôt GitHub
1. Crée un dépôt **public** (minutes GitHub Actions gratuites et illimitées).
2. Envoie-y tout le contenu de ce dossier.
3. *(optionnel)* Secret `NTFY_TOPIC` (Settings > Secrets and variables > Actions) pour les notifications,
   et variable `SITE_URL` avec l'adresse de ton site.

### 2. Les recherches à surveiller (`sources.json`)
Bien'ici est géré automatiquement (la zone Val-d'Oise est trouvée toute seule). Pour les autres sites :
1. Va sur le site, fais **une recherche** : achat, appartement, Val-d'Oise, **tri par plus récentes**.
   Laisse la fourchette de prix et de surface **large** : le filtrage fin se fait sur ton site.
2. Copie l'adresse de la page de résultats et colle-la dans `sources.json`, à la place du texte « COLLE ICI… ».
   Tu peux mettre plusieurs adresses par site (par exemple une recherche par tranche de prix).

Un site dont l'adresse n'est pas renseignée est simplement ignoré.

### 3. Le site
- **GitHub Pages** : Settings > Pages > *Deploy from a branch*, branche `main`, dossier `/ (root)`.
- **Netlify** : *Import from Git*, sans commande de build, dossier de publication `.`

### 4. Premier lancement du collecteur cloud
Onglet **Actions** > « Collecte des annonces » > *Run workflow* en cochant « Parcourir toutes les pages ».
Lis les journaux : tu dois voir la zone trouvée pour Bien'ici, puis le nombre d'annonces par page.
Ensuite le workflow tourne seul : toutes les 20 minutes en mode rapide, et chaque nuit en mode complet.

### 5. Le collecteur domicile (Leboncoin, SeLoger)
Prérequis : Python 3.10 ou plus, **Google Chrome** installé, et le dépôt cloné sur ton PC avec la
possibilité de faire `git push` (identifiants GitHub déjà configurés).

```
pip install -r collector/requirements-domicile.txt
python collector/collect_home.py --sans-push     # premier essai, sans publier
```

Une fenêtre Chrome s'ouvre. Si Leboncoin affiche un captcha, résous-le : la session est conservée
dans `collector/.profil-chrome/`, donc ça n'arrive en principe qu'une fois de temps en temps.
Quand le résultat te convient, lance sans `--sans-push` : le script met à jour
`data/annonces_domicile.json`, puis fait `git pull`, `commit` et `push`.

Pour l'automatiser, planifie `python collector/collect_home.py` toutes les 30 à 60 minutes :
planificateur de tâches Windows, `cron` sous Linux ou macOS. Le PC doit être allumé.
Mode sans fenêtre : option `--headless`, mais les sites le détectent plus souvent.

### 6. Les notifications (facultatif)
Chacun installe l'application **ntfy** (ou ouvre `https://ntfy.sh/<canal>`) et s'abonne au canal
choisi. Pas d'inscription. Le tout premier lancement n'envoie aucune notification.
Pour recevoir aussi les nouveautés de Leboncoin et SeLoger, définis `NTFY_TOPIC` sur ton PC.

## Ce qui a été vérifié, et ce qui ne l'a pas été

Les lecteurs (Bien'ici, PAP, Leboncoin), la fusion des doublons et le retrait des annonces disparues
ont été testés sur des **données simulées**. Les vrais sites n'ont **pas** pu être interrogés
pendant la conception. À surveiller au premier lancement :
- **Bien'ici** : la zone trouvée est bien le Val-d'Oise, et un lien d'annonce s'ouvre correctement
  (le lien est construit à partir de l'identifiant de l'annonce).
- **PAP** : le nombre d'annonces par page n'est pas 0. Sinon, c'est la structure de la page qu'il faut adapter.
- **Leboncoin** : le journal indique un nombre d'annonces par page. Sinon, la structure a changé.
- **SeLoger** : lecture « au mieux » du texte de la page, la plus susceptible de demander un réglage.

Si une source affiche 0 annonce, envoie le journal d'exécution (ou la page HTML enregistrée) et le lecteur sera adapté.

## Ajuster

| Je veux… | Je modifie… |
|---|---|
| limiter Bien'ici à une fourchette | `prix_min`, `prix_max`, `surface_min`, `surface_max` dans `sources.json` |
| changer la fréquence du cloud | les lignes `cron` de `.github/workflows/collect.yml` |
| retirer plus ou moins vite les annonces disparues | `CONSERVATION_JOURS` dans `collector/common.py` (14 jours par défaut) |
| couvrir plus de pages à chaque passage | `pages_max` dans `collect_cloud.py` et `collect_home.py` |

## Limites

- **Sources non officielles** : l'API de Bien'ici et les pages des autres sites ne sont pas faites pour cet usage.
  Elles peuvent changer sans prévenir, et leurs conditions d'utilisation n'autorisent pas forcément ce type
  de lecture automatique. Le site n'affiche que prix, surface, commune, titre et lien vers l'annonce d'origine,
  sans photos ni textes, et le collecteur reste modéré (une requête par seconde environ).
- Une annonce est retirée du site quand elle n'a plus été vue pendant 14 jours. Le passage complet de la nuit
  sert à ça pour Bien'ici. Une annonce située au-delà des pages parcourues peut disparaître alors qu'elle existe encore.
- Le site est à jour au rythme des collecteurs : 20 minutes pour Bien'ici et PAP, la cadence que tu choisis
  pour Leboncoin et SeLoger.
