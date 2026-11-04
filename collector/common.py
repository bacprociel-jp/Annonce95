"""Fonctions partagées par le collecteur cloud et le collecteur domicile."""
import hashlib
import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

RACINE = Path(__file__).resolve().parent.parent
# Une annonce non revue depuis ce nombre de jours est considérée comme retirée (vendue ou expirée)
CONSERVATION_JOURS = 14
HORS_ZONE = "hors-zone"

RE_PRIX = re.compile(r"(\d{1,3}(?:[ \u202f\u00a0.]\d{3})+|\d{5,7})\s*€")
RE_SURFACE = re.compile(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*m(?:²|2)(?![\w])", re.IGNORECASE)
RE_CP = re.compile(r"\b(\d{5})\b")


# --------------------------------------------------------------------------- utilitaires
def maintenant_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def nettoyer_url(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))


def entier(chaine: str) -> int:
    return int(re.sub(r"\D", "", chaine))


def hash_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:10]


# --------------------------------------------------------------------------- lecture générique (HTML)
def trouver_commune(texte: str, titre: str = ""):
    """Cherche un code postal 95xxx et prend le nom de commune juste avant."""
    if titre:
        texte = texte.replace(titre, " ")
    m = re.search(r"\b(95\d{3})\b", texte)
    if not m:
        return None, None
    avant = texte[: m.start()].rstrip(" (-,")
    morceau = re.split(r"[^A-Za-zÀ-ÿ'’\- ]", avant)[-1].strip(" -")
    mots = morceau.split()[-4:]
    commune = " ".join(mots) if mots and mots[0][:1].isupper() else (mots[-1] if mots else None)
    return commune or None, m.group(1)


def lire_bloc(texte: str, titre: str = ""):
    """Extrait prix, surface, code postal et commune d'un bloc de texte.
    Retourne None (infos insuffisantes) ou HORS_ZONE (autre département)."""
    texte = re.sub(r"\s+", " ", texte)
    m_prix = RE_PRIX.search(texte)
    m_surf = RE_SURFACE.search(texte)
    if not (m_prix and m_surf):
        return None
    prix = entier(m_prix.group(1))
    surface = float(m_surf.group(1).replace(",", "."))
    if not (20_000 <= prix <= 5_000_000 and 8 <= surface <= 500):
        return None
    commune, cp = trouver_commune(texte, titre)
    if not cp:
        autres = [c for c in RE_CP.findall(texte) if c != str(prix)]
        if any(not c.startswith("95") for c in autres[:1]):
            return HORS_ZONE
    return {"prix": prix, "surface": surface, "commune": commune, "cp": cp}


def extraire_annonces(html: str, source: str, base: str | None = None, motif: str | None = None):
    """Repère chaque lien d'annonce et lit le bloc de texte qui l'entoure.
    `motif` : morceau d'adresse que doit contenir un lien d'annonce (ex. "/annonce/")."""
    soupe = BeautifulSoup(html, "lxml")

    def href_de(a):
        h = a["href"].strip()
        if base:
            h = urljoin(base, h)
        return h

    def est_annonce(a):
        h = href_de(a)
        return h.startswith("http") and (not motif or motif in h)

    vus, resultats = set(), []
    for a in soupe.find_all("a", href=True):
        if not est_annonce(a):
            continue
        noeud = a
        for _ in range(7):
            noeud = noeud.parent
            if noeud is None:
                break
            texte = noeud.get_text(" ", strip=True)
            liens = {nettoyer_url(href_de(x)) for x in noeud.find_all("a", href=True) if est_annonce(x)}
            if len(texte) > 700 or len(liens) > 1:  # on a remonté jusqu'à plusieurs annonces
                break
            titre_lien = a.get_text(" ", strip=True)
            infos = lire_bloc(texte, titre_lien)
            if infos == HORS_ZONE:
                break
            if infos:
                url = nettoyer_url(href_de(a))
                if url in vus:
                    break
                vus.add(url)
                titre = titre_lien or texte[:80]
                infos.update({"url": url, "source": source, "titre": titre[:120]})
                resultats.append(infos)
                break
    return resultats


# --------------------------------------------------------------------------- fusion
def signature(a) -> str | None:
    """Même bien publié sur plusieurs sites : même prix, même surface, même code postal."""
    if a.get("cp"):
        return f"{a['prix']}|{round(a['surface'])}|{a['cp']}"
    return None


def fusionner(existantes, nouvelles, maintenant):
    """Ajoute les nouvelles annonces, fusionne les doublons, retire les annonces disparues.
    Retourne la liste des annonces réellement nouvelles."""
    aujourdhui = maintenant[:10]
    par_url = {}
    for a in existantes:
        par_url[a["url"]] = a
        for o in a.get("autres", []):
            par_url[o["url"]] = a
    par_signature = {signature(a): a for a in existantes if signature(a)}
    ajoutees = []
    for n in nouvelles:
        if n["url"] in par_url:
            par_url[n["url"]]["vu"] = aujourdhui
            continue
        sig = signature(n)
        if sig and sig in par_signature:
            doublon = par_signature[sig]
            doublon.setdefault("autres", [])
            if not any(o["url"] == n["url"] for o in doublon["autres"]):
                doublon["autres"].append({"source": n["source"], "url": n["url"]})
            doublon["vu"] = aujourdhui
            par_url[n["url"]] = doublon
            continue
        n["id"] = hash_id(n["url"])
        n["premier_vu"] = maintenant
        n["vu"] = aujourdhui
        existantes.append(n)
        par_url[n["url"]] = n
        if sig:
            par_signature[sig] = n
        ajoutees.append(n)
    limite = (datetime.fromisoformat(maintenant) - timedelta(days=CONSERVATION_JOURS)).date().isoformat()
    existantes[:] = [a for a in existantes if a.get("vu", a["premier_vu"][:10]) >= limite]
    return ajoutees


# --------------------------------------------------------------------------- fichiers
def charger(chemin: Path):
    if chemin.exists():
        return json.loads(chemin.read_text(encoding="utf-8")).get("annonces", [])
    return []


def sauver(chemin: Path, existantes, avant: str, maintenant: str) -> bool:
    existantes.sort(key=lambda a: a["premier_vu"], reverse=True)
    if json.dumps(existantes, sort_keys=True) == avant:
        return False
    chemin.write_text(
        json.dumps({"maj": maintenant, "annonces": existantes}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return True


# --------------------------------------------------------------------------- notifications
def _ntfy(corps: dict):
    req = urllib.request.Request("https://ntfy.sh", data=json.dumps(corps).encode(), method="POST")
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # une notification ratée ne doit pas casser la collecte
        print("ntfy :", e)


def notifier(ajoutees):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic or not ajoutees:
        return
    for a in ajoutees[:10]:  # limite anti-spam
        lieu = a.get("commune") or a.get("cp") or "Val-d'Oise"
        _ntfy({
            "topic": topic,
            "title": f"{a['prix']:,} € · {a['surface']:g} m² · {lieu}".replace(",", " "),
            "message": f"{a['source']} : {a['titre']}",
            "click": a["url"],
            "tags": ["house"],
        })
    if len(ajoutees) > 10:
        _ntfy({
            "topic": topic,
            "title": f"+ {len(ajoutees) - 10} autres annonces",
            "message": "Voir la liste complète sur le site.",
            "click": os.environ.get("SITE_URL") or "https://ntfy.sh",
        })
