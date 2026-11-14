#!/usr/bin/env python3
"""Collecteur cloud (GitHub Actions) :
  - interroge directement Bien'ici ;
  - lit les alertes e-mail des sites qui bloquent la lecture automatique (PAP, Leboncoin, SeLoger),
    si IMAP_USER et IMAP_PASSWORD sont définis.

Écrit data/annonces.json. Les adresses de recherche viennent de sources.json.
Variables d'environnement facultatives :
  FULL=1       parcourt toutes les pages (sinon : s'arrête dès qu'une page n'apporte rien de nouveau)
  NTFY_TOPIC   canal ntfy pour les notifications
  SITE_URL     adresse du site, ajoutée aux notifications
  IMAP_USER, IMAP_PASSWORD   boîte Gmail dédiée aux alertes (mot de passe d'application)
"""
import email
import imaplib
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr
from urllib.parse import quote

from common import (RACINE, RE_PRIX, charger, entier, extraire_annonces, fusionner, maintenant_iso,
                    notifier, sauver)

DATA = RACINE / "data" / "annonces.json"
CONFIG = RACINE / "sources.json"
UA = "Mozilla/5.0 (compatible; annonces95/2.0; usage personnel)"
PAUSE = 1.0  # secondes entre deux requêtes, pour rester poli
BIENICI = "https://www.bienici.com"


def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=30) as rep:
        return rep.read().decode("utf-8", errors="replace")


# =========================================================================== Bien'ici
def resoudre_zone(nom: str = "Val-d'Oise"):
    """Demande à Bien'ici l'identifiant de zone du département."""
    donnees = json.loads(http_get(f"{BIENICI}/suggest.json?q={quote(nom)}"))
    candidats = [d for d in donnees if isinstance(d, dict) and d.get("zoneIds")]
    if not candidats:
        raise RuntimeError(f"Bien'ici n'a pas reconnu « {nom} » (réponse : {str(donnees)[:200]})")
    choix = next((d for d in candidats if "val-d" in str(d.get("name", "")).lower()), candidats[0])
    print(f"  Zone Bien'ici : {choix.get('name')} → {choix['zoneIds']}")
    return choix["zoneIds"]


def premier_nombre(valeur):
    if isinstance(valeur, list):
        valeur = min((v for v in valeur if isinstance(v, (int, float))), default=None)
    return valeur if isinstance(valeur, (int, float)) else None


def lire_reponse_bienici(donnees: dict):
    """Transforme la réponse JSON de Bien'ici en annonces normalisées."""
    res = []
    for ad in donnees.get("realEstateAds", []):
        prix, surface = premier_nombre(ad.get("price")), premier_nombre(ad.get("surfaceArea"))
        ident = ad.get("id")
        if not (ident and prix and surface):
            continue
        cp = str(ad.get("postalCode") or "")
        if cp and not cp.startswith("95"):
            continue
        pieces = ad.get("roomsQuantity")
        titre = ad.get("title") or (f"Appartement {pieces} pièces" if pieces else "Appartement")
        res.append({
            "url": f"{BIENICI}/annonce/{ident}",
            "source": "Bien'ici",
            "titre": str(titre)[:120],
            "prix": int(prix),
            "surface": float(surface),
            "commune": ad.get("city"),
            "cp": cp or None,
        })
    return res


PLAFOND = 2500  # Bien'ici ne sert jamais plus de 2500 résultats pour une même recherche
TAILLE = 100
# Tranches de prix pour le passage complet : le département dépasse le plafond en une seule recherche
BORNES = [0, 100_000, 150_000, 200_000, 250_000, 300_000, 400_000, 600_000, None]


def requete_bienici(zones, cfg, debut, prix_min=None, prix_max=None):
    filtres = {
        "size": TAILLE, "from": debut, "showAllModels": False, "filterType": "buy",
        "propertyType": ["flat"], "onTheMarket": [True],
        "sortBy": "publicationDate", "sortOrder": "desc",
        "zoneIdsByTypes": {"zoneIds": zones},
    }
    if prix_min:
        filtres["minPrice"] = prix_min
    if prix_max:
        filtres["maxPrice"] = prix_max
    for cle_cfg, cle_api in (("surface_min", "minArea"), ("surface_max", "maxArea")):
        if cfg.get(cle_cfg):
            filtres[cle_api] = cfg[cle_cfg]
    url = f"{BIENICI}/realEstateAds.json?filters={quote(json.dumps(filtres, separators=(',', ':')))}"
    return json.loads(http_get(url))


def parcourir(zones, cfg, connus, complet, premiere, prix_min=None, prix_max=None):
    """Lit les pages d'une recherche, sans jamais dépasser le plafond de Bien'ici.
    Une erreur en cours de route ne fait pas perdre ce qui a déjà été lu."""
    resultat, donnees, debut = [], premiere, 0
    while True:
        annonces = lire_reponse_bienici(donnees)
        resultat.extend(annonces)
        print(f"  Bien'ici : {len(annonces)} annonces (de {debut} à {debut + TAILLE}, "
              f"total annoncé : {donnees.get('total')})")
        debut += TAILLE
        if not donnees.get("realEstateAds") or debut >= (donnees.get("total") or 0) or debut >= PLAFOND:
            break
        if not complet and not any(a["url"] not in connus for a in annonces):
            break  # tri du plus récent au plus ancien : plus rien de neuf plus loin
        time.sleep(PAUSE)
        try:
            donnees = requete_bienici(zones, cfg, debut, prix_min, prix_max)
        except Exception as e:
            print(f"  Avertissement : arrêt à {debut} annonces ({e!r}). Résultats conservés.")
            break
    return resultat


def collecter_bienici(cfg: dict, connus: set, complet: bool):
    zones = cfg.get("zone_ids") or resoudre_zone()
    if not complet:
        return parcourir(zones, cfg, connus, False, requete_bienici(zones, cfg, 0))

    # Passage complet : on découpe par tranches de prix pour contourner le plafond de 2500
    bas, haut = cfg.get("prix_min") or 0, cfg.get("prix_max")
    bornes = [b for b in BORNES if b is not None and b > bas and (haut is None or b < haut)]
    bornes = [bas] + bornes + [haut]
    tranches = list(zip(bornes[:-1], bornes[1:]))
    total_global = (requete_bienici(zones, cfg, 0, bas or None, haut).get("total") or 0)
    print(f"  Bien'ici : {total_global} annonces au total pour le département.")
    resultat, filtre_ignore = [], []

    def explorer(lo, hi, profondeur):
        if filtre_ignore:
            return
        donnees = requete_bienici(zones, cfg, 0, lo or None, hi)
        total = donnees.get("total") or 0
        print(f"  Tranche {lo} à {hi if hi else '∞'} € : {total} annonces")
        if (lo or hi) and total >= total_global and (lo, hi) != (bas, haut):
            # le filtre de prix n'est pas pris en compte : inutile de découper davantage
            print("  Avertissement : Bien'ici ignore le filtre de prix, lecture limitée à 2500 annonces.")
            filtre_ignore.append(True)
            resultat.extend(parcourir(zones, cfg, connus, True, donnees, lo or None, hi))
            return
        if total > PLAFOND - TAILLE and hi is not None and hi - lo > 2000 and profondeur < 6:
            milieu = (lo + hi) // 2
            explorer(lo, milieu, profondeur + 1)
            explorer(milieu, hi, profondeur + 1)
            return
        resultat.extend(parcourir(zones, cfg, connus, True, donnees, lo or None, hi))
        time.sleep(PAUSE)

    for lo, hi in tranches:
        explorer(lo, hi, 0)
    return resultat


# =========================================================================== alertes e-mail
NOMS_EMAIL = {"pap.fr": "PAP", "leboncoin": "Leboncoin", "seloger": "SeLoger", "logic-immo": "Logic-Immo",
              "paruvendu": "ParuVendu", "bienici": "Bien'ici"}
MOTIFS_EMAIL = {"PAP": "/annonces/"}  # les fiches PAP ont des adresses en /annonces/…
# Types de biens à écarter (les alertes PAP peuvent contenir des maisons ou des terrains)
EXCLUSIONS_EMAIL = {"PAP": ("/annonces/maison", "/annonces/terrain", "/annonces/immeuble")}
# Biens autres que des appartements, repérés au sujet de l'e-mail ou au titre de l'annonce
RE_AUTRE_BIEN = re.compile(r"^\s*(?:alerte e-?mail\s*:\s*)?(maison|terrain|villa|immeuble|parking|local)\b", re.I)


def decoder(valeur: str) -> str:
    try:
        return str(make_header(decode_header(valeur or "")))
    except Exception:
        return valeur or ""


def corps_html(msg) -> str:
    texte = ""
    for part in msg.walk():
        type_ = part.get_content_type()
        if type_ not in ("text/html", "text/plain"):
            continue
        try:
            contenu = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if type_ == "text/html":
            return contenu
        texte += contenu
    return texte


# --- SeLoger : un bloc (tableau) par annonce, tous les liens passent par click.by.seloger.com
RE_PIECES_SURFACE = re.compile(r"(\d+)\s*pi[èe]ces?\s*[·•.\-]\s*(\d+(?:[.,]\d+)?)\s*m(?:²|2)", re.I)
RE_LIEU = re.compile(r"^\s*(?:[^,()]+,\s*)?([^,()]+?)\s*\((\d{5})\)")
RE_EURO_M2 = re.compile(r"€\s*/\s*m\s*(?:²|2)")


def bloc_seloger(noeud):
    """Remonte depuis la mention « €/m² » jusqu'au plus petit bloc qui décrit toute l'annonce."""
    n = noeud.parent
    for _ in range(12):
        if n is None:
            return None, ""
        texte = re.sub(r"\s+", " ", n.get_text(" ", strip=True))
        if RE_PIECES_SURFACE.search(texte) and re.search(r"\(\d{5}\)", texte):
            return n, texte
        n = n.parent
    return None, ""


def extraire_seloger(html: str):
    """Lit les annonces d'un e-mail d'alerte SeLoger.
    L'adresse reste celle du lien de suivi : on ne retire surtout pas son paramètre qs."""
    from bs4 import BeautifulSoup
    soupe = BeautifulSoup(html, "lxml")
    blocs, resultat = set(), []
    for t in soupe.find_all(string=RE_EURO_M2):
        bloc, texte = bloc_seloger(t)
        if bloc is None or id(bloc) in blocs:
            continue
        blocs.add(id(bloc))
        m_prix = RE_PRIX.search(texte)
        m_surf = list(RE_PIECES_SURFACE.finditer(texte))
        if not (m_prix and m_surf):
            continue
        m_surf = m_surf[-1]
        prix = entier(m_prix.group(1))
        surface = float(m_surf.group(2).replace(",", "."))
        m_lieu = RE_LIEU.search(texte[m_surf.end():])
        commune, cp = (m_lieu.group(1).strip(), m_lieu.group(2)) if m_lieu else (None, None)
        if cp and not cp.startswith("95"):
            continue
        liens = [a for a in bloc.find_all("a", href=True) if a["href"].startswith("http")]
        lien = next((a for a in liens if "voir l" in a.get_text(" ", strip=True).lower()), None) \
            or (liens[0] if liens else None)
        if not lien:
            continue
        m_m2 = RE_EURO_M2.search(texte)
        titre = texte[m_m2.end():m_surf.start()].strip() if m_m2 else ""
        resultat.append({
            "url": lien["href"].strip(), "source": "SeLoger", "titre": (titre or "Appartement")[:120],
            "prix": prix, "surface": surface, "commune": commune, "cp": cp,
        })
    return resultat


def url_invalide(url: str) -> bool:
    """Adresse inutilisable : lien de suivi SeLoger privé de son paramètre (ancienne version du lecteur)."""
    return url.rstrip("/") in ("https://click.by.seloger.com", "http://click.by.seloger.com")


def analyser_message(msg):
    expediteur = parseaddr(decoder(msg.get("From", "")))[1].lower()
    source = next((nom for cle, nom in NOMS_EMAIL.items() if cle in expediteur),
                  expediteur.split("@")[-1].split(".")[0].capitalize() or "Autre")
    sujet = decoder(msg.get("Subject", ""))
    if source == "SeLoger":
        annonces = extraire_seloger(corps_html(msg))
    else:
        annonces = extraire_annonces(corps_html(msg), source, motif=MOTIFS_EMAIL.get(source))
    exclus = EXCLUSIONS_EMAIL.get(source, ())
    annonces = [a for a in annonces if not any(x in a["url"] for x in exclus)]
    if source == "PAP":
        if RE_AUTRE_BIEN.match(sujet):
            annonces = []
        else:
            annonces = [a for a in annonces if not RE_AUTRE_BIEN.match(a["titre"])]
    print(f"  E-mail {source:<10} {len(annonces)} annonce(s) : {sujet[:60]}")
    return annonces


def dossiers_speciaux(imap) -> dict:
    """Repère, par leurs attributs IMAP, le dossier « Tous les messages » et le dossier Spam."""
    res = {}
    try:
        _, lignes = imap.list()
    except Exception:
        return res
    for brut in lignes or []:
        texte = brut.decode("utf-8", errors="replace") if isinstance(brut, bytes) else str(brut)
        m = re.match(r'\((?P<attrs>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<nom>.+)$', texte)
        if not m:
            continue
        nom = m.group("nom").strip().strip('"')
        attrs = m.group("attrs").lower()
        if "\\all" in attrs:
            res["tous"] = nom
        elif "\\junk" in attrs:
            res["spam"] = nom
    return res


def diagnostiquer_absents(imap, absents, depuis):
    """Pour chaque expéditeur sans e-mail en boîte de réception, regarde s'il en existe ailleurs.
    N'affiche que des nombres : le journal d'un dépôt public est lisible par tout le monde."""
    try:
        dossiers = dossiers_speciaux(imap)
        for exp in absents:
            print(f"  Aucun e-mail « {exp} » dans la boîte de réception.")
            for etiquette, cle in (("Tous les messages (archivés ou classés hors boîte de réception)", "tous"),
                                   ("Spam", "spam")):
                nom = dossiers.get(cle)
                if not nom:
                    continue
                if imap.select('"' + nom + '"', readonly=True)[0] != "OK":
                    continue
                _, data = imap.search(None, f'(SINCE "{depuis}" FROM "{exp}")')
                print(f"    {len(data[0].split())} e-mail(s) « {exp} » dans : {etiquette}")
    except Exception as e:  # un diagnostic raté ne doit jamais casser la collecte
        print("  Diagnostic des dossiers impossible :", repr(e))


def lire_emails(cfg_mail: dict):
    expediteurs = cfg_mail.get("expediteurs") or ["pap.fr"]
    depuis = (datetime.now() - timedelta(days=int(cfg_mail.get("jours", 3)))).strftime("%d-%b-%Y")
    # Les secrets collés contiennent souvent une espace ou un retour à la ligne invisible : on les retire.
    utilisateur = os.environ["IMAP_USER"].strip()
    mot_de_passe = "".join(os.environ["IMAP_PASSWORD"].split())
    print(f"  Identifiants lus : adresse de {len(utilisateur)} caractères (domaine « {utilisateur.split('@')[-1]} »), "
          f"mot de passe de {len(mot_de_passe)} caractères (un mot de passe d'application en fait 16).")
    imap = imaplib.IMAP4_SSL(os.environ.get("IMAP_HOST", "imap.gmail.com"))
    try:
        imap.login(utilisateur, mot_de_passe)
    except imaplib.IMAP4.error as e:
        print("  Gmail refuse la connexion. Vérifie : mot de passe d'application créé avec CE compte, "
              "adresse complète dans IMAP_USER, alerte de sécurité Google éventuelle dans la boîte.")
        raise
    imap.select("INBOX", readonly=True)
    ids, absents = set(), []
    for exp in expediteurs:
        _, data = imap.search(None, f'(SINCE "{depuis}" FROM "{exp}")')
        trouves = data[0].split()
        ids.update(trouves)
        if not trouves:
            absents.append(exp)
    print(f"  {len(ids)} e-mail(s) d'alerte des {depuis} à aujourd'hui.")
    if absents:
        diagnostiquer_absents(imap, absents, depuis)
        imap.select("INBOX", readonly=True)
    annonces = []
    for num in sorted(ids):
        _, data = imap.fetch(num, "(BODY.PEEK[])")
        annonces.extend(analyser_message(email.message_from_bytes(data[0][1])))
    imap.logout()
    return annonces


# =========================================================================== principal
def main():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    existantes = charger(DATA)
    complet = bool(os.environ.get("FULL")) or not existantes  # 1er lancement : tout lire
    avant = json.dumps(existantes, sort_keys=True)
    invalides = [a for a in existantes if url_invalide(a["url"])]
    if invalides:
        print(f"  {len(invalides)} annonce(s) SeLoger sans lien exploitable retirée(s) (ancienne lecture).")
        existantes[:] = [a for a in existantes if not url_invalide(a["url"])]
    connus = {a["url"] for a in existantes} | {o["url"] for a in existantes for o in a.get("autres", [])}
    maintenant = maintenant_iso()
    print(f"Mode {'complet' if complet else 'rapide'}, {len(existantes)} annonces déjà connues.")

    nouvelles, actives, echecs = [], 0, 0
    cfg_bienici = cfg.get("bienici", {})
    if cfg_bienici.get("actif", True):
        actives += 1
        try:
            nouvelles += collecter_bienici(cfg_bienici, connus, complet)
        except Exception as e:
            echecs += 1
            print("ERREUR Bien'ici :", repr(e))

    cfg_mail = cfg.get("emails")
    if cfg_mail and os.environ.get("IMAP_USER") and os.environ.get("IMAP_PASSWORD"):
        actives += 1
        try:
            nouvelles += lire_emails(cfg_mail)
        except Exception as e:
            echecs += 1
            print("ERREUR alertes e-mail :", repr(e))

    if actives and echecs == actives:
        sys.exit("Toutes les sources ont échoué : aucune mise à jour (rien n'est retiré).")

    premiere_fois = not existantes
    ajoutees = fusionner(existantes, nouvelles, maintenant)
    print(f"{len(nouvelles)} annonce(s) lue(s), {len(ajoutees)} nouvelle(s).")
    if sauver(DATA, existantes, avant, maintenant):
        print("Fichier mis à jour.")
    if not premiere_fois:  # pas de rafale de notifications au tout premier lancement
        notifier(ajoutees)


if __name__ == "__main__":
    main()
