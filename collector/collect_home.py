#!/usr/bin/env python3
"""Collecteur domicile : lit Leboncoin et SeLoger avec un vrai Chrome, depuis ta connexion Internet.

Pourquoi chez toi ? Ces deux sites bloquent les serveurs (dont GitHub Actions) mais acceptent
en général un navigateur normal sur une connexion domestique.

Écrit data/annonces_domicile.json, puis le publie sur GitHub (git pull / commit / push).

Utilisation :
  python collector/collect_home.py                 # collecte + publication
  python collector/collect_home.py --sans-push     # collecte seulement
  python collector/collect_home.py --headless      # sans fenêtre (plus souvent bloqué)
  python collector/collect_home.py --complet       # parcourt toutes les pages configurées
"""
import argparse
import json
import random
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from common import (RACINE, charger, extraire_annonces, fusionner, maintenant_iso, nettoyer_url,
                    notifier, sauver)

DATA = RACINE / "data" / "annonces_domicile.json"
CONFIG = RACINE / "sources.json"
PROFIL = RACINE / "collector" / ".profil-chrome"  # cookies conservés d'un passage à l'autre


# =========================================================================== Leboncoin
def trouver_ads(obj):
    """Cherche récursivement la liste d'annonces dans le JSON __NEXT_DATA__."""
    if isinstance(obj, dict):
        ads = obj.get("ads")
        if isinstance(ads, list) and ads and isinstance(ads[0], dict) and "list_id" in ads[0]:
            return ads
        for v in obj.values():
            r = trouver_ads(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = trouver_ads(v)
            if r:
                return r
    return None


def nombre(valeur):
    if isinstance(valeur, list):
        valeur = valeur[0] if valeur else None
    try:
        return float(str(valeur).replace(",", ".").split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def lire_leboncoin(html: str):
    """Lit les annonces dans le JSON embarqué de la page (méthode fiable si présent)."""
    soupe = BeautifulSoup(html, "lxml")
    balise = soupe.find("script", id="__NEXT_DATA__")
    if not balise or not balise.string:
        return None
    ads = trouver_ads(json.loads(balise.string)) or []
    res = []
    for ad in ads:
        prix = nombre(ad.get("price"))
        attributs = {a.get("key"): a.get("value") for a in ad.get("attributes", []) if isinstance(a, dict)}
        surface = nombre(attributs.get("square"))
        loc = ad.get("location") or {}
        cp = str(loc.get("zipcode") or "")
        url = ad.get("url")
        if not (prix and surface and url):
            continue
        if cp and not cp.startswith("95"):
            continue
        res.append({
            "url": nettoyer_url(url),
            "source": "Leboncoin",
            "titre": str(ad.get("subject") or "Appartement")[:120],
            "prix": int(prix),
            "surface": surface,
            "commune": loc.get("city"),
            "cp": cp or None,
        })
    return res


# =========================================================================== navigation
def avec_page(url: str, n: int) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query))
    q["page"] = str(n)
    return urlunparse(p._replace(query=urlencode(q)))


def bloque(page) -> bool:
    return "captcha-delivery.com" in page.content() or "captcha" in page.url.lower()


def ouvrir(page, url: str, sans_fenetre: bool) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2500)
    if bloque(page):
        if sans_fenetre:
            raise RuntimeError("page bloquée (captcha) : relance sans --headless")
        print("  Captcha détecté : résous-le dans la fenêtre de Chrome (3 minutes maximum)…")
        fin = time.time() + 180
        while time.time() < fin and bloque(page):
            page.wait_for_timeout(3000)
        if bloque(page):
            raise RuntimeError("captcha non résolu")
        page.wait_for_timeout(1500)
    return page.content()


def collecter_leboncoin(page, urls, connus, complet, sans_fenetre):
    pages_max = 6 if complet else 2
    res = []
    for depart in urls:
        for n in range(1, pages_max + 1):
            url = depart if n == 1 else avec_page(depart, n)
            html = ouvrir(page, url, sans_fenetre)
            annonces = lire_leboncoin(html)
            if annonces is None:  # pas de JSON embarqué : lecture générique du texte
                annonces = extraire_annonces(html, "Leboncoin", base=url, motif="/ad/")
            print(f"  Leboncoin : {len(annonces)} annonces (page {n})")
            res.extend(annonces)
            if not annonces or (not complet and not any(a["url"] not in connus for a in annonces)):
                break
            time.sleep(random.uniform(3, 7))
    return res


def collecter_seloger(page, urls, sans_fenetre):
    res = []
    for url in urls:
        html = ouvrir(page, url, sans_fenetre)
        page.mouse.wheel(0, 4000)  # déclenche le chargement des annonces plus bas
        page.wait_for_timeout(1500)
        annonces = extraire_annonces(page.content(), "SeLoger", base=url)
        print(f"  SeLoger : {len(annonces)} annonces")
        res.extend(annonces)
        time.sleep(random.uniform(3, 7))
    return res


# =========================================================================== publication
def git(*args):
    return subprocess.run(["git", *args], cwd=RACINE, capture_output=True, text=True)


def publier():
    git("pull", "--rebase", "--autostash")
    git("add", str(DATA))
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("Rien à publier.")
        return
    git("commit", "-m", "Mise à jour des annonces (domicile)")
    sortie = git("push")
    print("Publié sur GitHub." if sortie.returncode == 0 else f"ÉCHEC du push : {sortie.stderr.strip()}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sans-push", action="store_true")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--complet", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    urls_lbc = [u for u in cfg.get("leboncoin", []) if u.startswith("http")]
    urls_sl = [u for u in cfg.get("seloger", []) if u.startswith("http")]
    if not (urls_lbc or urls_sl):
        sys.exit("Aucune adresse Leboncoin ou SeLoger dans sources.json.")

    existantes = charger(DATA)
    avant = json.dumps(existantes, sort_keys=True)
    connus = {a["url"] for a in existantes} | {o["url"] for a in existantes for o in a.get("autres", [])}
    maintenant = maintenant_iso()
    nouvelles, actives, echecs = [], 0, 0

    PROFIL.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFIL), channel="chrome", headless=args.headless, locale="fr-FR",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for nom, fonction, urls in (
            ("Leboncoin", lambda u: collecter_leboncoin(page, u, connus, args.complet, args.headless), urls_lbc),
            ("SeLoger", lambda u: collecter_seloger(page, u, args.headless), urls_sl),
        ):
            if not urls:
                continue
            actives += 1
            try:
                nouvelles += fonction(urls)
            except Exception as e:
                echecs += 1
                print(f"ERREUR {nom} :", repr(e))
        ctx.close()

    if actives and echecs == actives:
        sys.exit("Toutes les sources ont échoué : aucune mise à jour (rien n'est retiré).")

    premiere_fois = not existantes
    ajoutees = fusionner(existantes, nouvelles, maintenant)
    print(f"{len(nouvelles)} annonce(s) lue(s), {len(ajoutees)} nouvelle(s).")
    if sauver(DATA, existantes, avant, maintenant):
        print("Fichier mis à jour.")
    if not premiere_fois:
        notifier(ajoutees)
    if not args.sans_push:
        publier()


if __name__ == "__main__":
    main()
